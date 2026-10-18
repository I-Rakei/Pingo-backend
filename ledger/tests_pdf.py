"""PDFs are generated on the server from the ledger, stored briefly, and swept by a periodic job."""

import base64
import os
import re
import tempfile
import time
import zlib
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from ledger import pdf_store
from ledger.models import Organization, OrganizationMembership


def pdf_text(data):
    """The text strings drawn on a PDF's pages, decoded from its own content streams."""
    pieces = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        raw = match.group(1).strip()
        if raw.endswith(b"~>"):
            raw = base64.a85decode(raw[:-2])
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        for item in re.findall(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", raw):
            item = re.sub(rb"\\([0-7]{3})", lambda octal: bytes([int(octal.group(1), 8)]), item)
            pieces.append(re.sub(rb"\\(.)", rb"\1", item).decode("cp1252"))
    return "\n".join(pieces)


class PdfTestCase(TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        override = override_settings(GENERATED_PDF_DIR=Path(self.storage.name))
        override.enable()
        self.addCleanup(override.disable)

    def user_api(self, email):
        user = User.objects.create_user(username=email, email=email, password="very-secret", first_name="Ismael", last_name="Rakei")
        api = APIClient()
        api.force_authenticate(user)
        return user, api

    def make_debt(self, api, name="Estrela David", principal="7000", rate="30", months=3, loan_type="multi"):
        client_id = api.post("/api/clients/", {"name": name, "phone": "+258 84 000 0000"}, format="json").data["id"]
        total = {"7000": "9100.00"}.get(principal, principal)
        response = api.post("/api/debts/", {
            "clientId": client_id, "name": name, "loanType": loan_type, "principal": principal, "interestRate": rate,
            "penaltyRate": "5", "durationMonths": months, "total": total, "startDate": "2099-01-01", "dueDate": "2099-04-01",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"], client_id

    def generate(self, api, path, payload, expected=201):
        response = api.post(path, payload, format="json")
        self.assertEqual(response.status_code, expected, getattr(response, "data", None))
        return response

    def fetch(self, api, url):
        """GET a file; the response is closed afterwards so the file handle is released (Windows cannot delete open files)."""
        response = api.get(url)
        self.addCleanup(response.close)
        return response

    def download(self, api, response):
        file_response = self.fetch(api, response.data["url"])
        self.assertEqual(file_response.status_code, 200)
        return b"".join(file_response.streaming_content)


class DebtDocumentTests(PdfTestCase):
    def setUp(self):
        super().setUp()
        self.user, self.api = self.user_api("pdf@example.com")
        self.reference, self.client_id = self.make_debt(self.api)

    def test_invoice_is_generated_stored_and_downloadable(self):
        response = self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference, "kind": "invoice", "language": "en"})
        self.assertEqual(response.data["filename"], f"pingo-invoice-{self.reference}.pdf")
        self.assertEqual(response.data["url"], f"/api/pdf/files/{response.data['id']}/")
        file_response = self.fetch(self.api, response.data["url"])
        self.assertEqual(file_response["Content-Type"], "application/pdf")
        self.assertIn(f'filename="pingo-invoice-{self.reference}.pdf"', file_response["Content-Disposition"])
        self.assertIn("no-store", file_response["Cache-Control"])
        data = b"".join(file_response.streaming_content)
        self.assertTrue(data.startswith(b"%PDF"))

    def test_invoice_adds_up_to_the_ledger_to_the_cent(self):
        text = pdf_text(self.download(self.api, self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference, "kind": "invoice", "language": "en"})))
        for expected in ("INVOICE", "3,033.00 MZN", "3,034.00 MZN", "9,100.00 MZN", "Estrela David", "Page 1 of 1"):
            self.assertIn(expected, text)
        self.assertNotIn("9,099.99", text)
        self.assertEqual(text.count("3,033.00 MZN"), 2)

    def test_the_document_follows_the_requested_language(self):
        text = pdf_text(self.download(self.api, self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference, "language": "pt"})))
        for expected in ("FACTURA", "Prestação 1", "9100,00 MZN", "Por pagar", "MONTANTES A PAGAR", "Gerado com Pingo"):
            self.assertIn(expected, text)

    def test_payment_and_period_receipts(self):
        self.generate(self.api, f"/api/debts/{self.reference}/payments/", {"action": "balance", "amount": "3033.00", "installmentNumber": 1}, expected=200)
        payments = self.api.get("/api/payments/").data
        payment_id = payments[0]["id"]
        receipt = self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference, "kind": "receipt", "paymentId": payment_id, "language": "en"})
        self.assertEqual(receipt.data["filename"], f"pingo-receipt-{self.reference}-{payment_id[:6].upper()}.pdf")
        text = pdf_text(self.download(self.api, receipt))
        for expected in ("RECEIPT", "3,033.00 MZN", "Payment for period 1", "6,067.00 MZN"):
            self.assertIn(expected, text)

        period = self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference, "kind": "receipt", "installmentNumber": 1, "language": "en"})
        self.assertEqual(period.data["filename"], f"pingo-receipt-{self.reference}-P1.pdf")
        everything = self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference, "kind": "receipt", "language": "en"})
        self.assertEqual(everything.data["filename"], f"pingo-receipt-{self.reference}.pdf")

    def test_unknown_records_and_bad_input_are_rejected(self):
        self.generate(self.api, "/api/pdf/debt/", {"reference": "PNG-9999"}, expected=404)
        self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference, "kind": "receipt", "paymentId": "11111111-1111-1111-1111-111111111111"}, expected=404)
        self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference, "kind": "receipt", "installmentNumber": 99}, expected=404)
        self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference, "language": "fr"}, expected=400)
        self.generate(self.api, "/api/pdf/debt/", {"kind": "invoice"}, expected=400)
        self.assertEqual(APIClient().post("/api/pdf/debt/", {"reference": self.reference}, format="json").status_code, 403)

    def test_one_account_cannot_generate_or_fetch_another_accounts_documents(self):
        _, other_api = self.user_api("other@example.com")
        self.generate(other_api, "/api/pdf/debt/", {"reference": self.reference}, expected=404)
        mine = self.generate(self.api, "/api/pdf/debt/", {"reference": self.reference})
        self.assertEqual(self.fetch(other_api, mine.data["url"]).status_code, 404)
        self.assertEqual(self.fetch(APIClient(), mine.data["url"]).status_code, 403)
        self.assertEqual(self.fetch(self.api, mine.data["url"]).status_code, 200)

    def test_single_loan_invoice_lists_interest_then_capital(self):
        reference, _ = self.make_debt(self.api, name="Carlos", principal="18500", rate="8", months=1, loan_type="single")
        text = pdf_text(self.download(self.api, self.generate(self.api, "/api/pdf/debt/", {"reference": reference, "language": "en"})))
        for expected in ("Interest, period 1", "1,480.00 MZN", "Capital", "18,500.00 MZN", "19,980.00 MZN"):
            self.assertIn(expected, text)

    def test_a_long_receipt_flows_onto_more_pages_with_numbering(self):
        reference, _ = self.make_debt(self.api, name="Muitas", principal="12000", rate="10", months=24)
        text = pdf_text(self.download(self.api, self.generate(self.api, "/api/pdf/debt/", {"reference": reference, "kind": "invoice", "language": "en"})))
        self.assertIn("Instalment 24", text)
        self.assertRegex(text, r"Page 1 of [2-9]")


class CorporateDocumentTests(PdfTestCase):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(username="corp@example.com", email="corp@example.com", password="very-secret", first_name="Owner")
        self.organization = Organization.objects.create(name="Doc Corp", nuit="400123456", address="Av. 24 de Julho, Maputo", created_by=self.owner)
        OrganizationMembership.objects.create(user=self.owner, organization=self.organization, role=OrganizationMembership.Role.OWNER)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.reference, self.client_id = self.make_debt(self.api, name="Fatima")

    def test_fiscal_documents_carry_their_own_numbering_and_details(self):
        self.api.post("/api/organizations/documents/invoice/", {"clientId": self.client_id, "amount": "100.00", "description": "Setup fee"}, format="json")
        credit = self.api.post("/api/organizations/documents/credit_note/", {"clientId": self.client_id, "amount": "25.00", "description": "Goodwill", "reason": "Late delivery of the loan"}, format="json")
        self.assertEqual(credit.status_code, 201, credit.data)
        invoice = self.generate(self.api, "/api/pdf/fiscal/", {"type": "invoice", "number": 1, "language": "en"})
        self.assertEqual(invoice.data["filename"], "pingo-invoice-1.pdf")
        text = pdf_text(self.download(self.api, invoice))
        for expected in ("FT 0001", "Doc Corp", "NUIT 400123456", "Setup fee", "100.00 MZN", "Fatima"):
            self.assertIn(expected, text)
        note = pdf_text(self.download(self.api, self.generate(self.api, "/api/pdf/fiscal/", {"type": "credit_note", "number": 1, "language": "pt"})))
        for expected in ("NOTA DE CR", "NC 0001", "Late delivery of the loan", "25,00 MZN", "CREDITADO A"):
            self.assertIn(expected, note)
        self.generate(self.api, "/api/pdf/fiscal/", {"type": "invoice", "number": 99}, expected=404)
        self.generate(self.api, "/api/pdf/fiscal/", {"type": "receipt", "number": 1}, expected=400)

    def test_fiscal_documents_stay_inside_the_organization(self):
        self.api.post("/api/organizations/documents/invoice/", {"clientId": self.client_id, "amount": "100.00"}, format="json")
        outsider = User.objects.create_user(username="out@example.com", email="out@example.com", password="very-secret")
        other_org = Organization.objects.create(name="Other Corp", created_by=outsider)
        OrganizationMembership.objects.create(user=outsider, organization=other_org, role=OrganizationMembership.Role.OWNER)
        other_api = APIClient()
        other_api.force_authenticate(outsider)
        self.generate(other_api, "/api/pdf/fiscal/", {"type": "invoice", "number": 1}, expected=404)

    def test_schedules_for_a_recorded_loan_a_saved_plan_and_a_simulation(self):
        recorded = self.generate(self.api, "/api/pdf/schedule/", {"source": "recorded", "reference": self.reference, "language": "en"})
        self.assertEqual(recorded.data["filename"], f"pingo-amortization-{self.reference}.pdf")
        text = pdf_text(self.download(self.api, recorded))
        for expected in ("AMORTIZATION PLAN", self.reference, "3,033.00", "3,034.00", "9,100.00 MZN", "Ledger schedule only"):
            self.assertIn(expected, text)

        terms = {"loanType": "multi", "principal": "100.00", "interestRate": "10", "durationMonths": 3, "startDate": "2099-01-31", "clientName": "Ana"}
        simulation = self.generate(self.api, "/api/pdf/schedule/", {"source": "simulation", "terms": terms, "language": "en"})
        self.assertEqual(simulation.data["filename"], "pingo-amortization-simulation.pdf")
        text = pdf_text(self.download(self.api, simulation))
        for expected in ("SIMULATION", "Ana", "36.00", "38.00", "110.00 MZN", "Simulation only"):
            self.assertIn(expected, text)

        plan = self.api.post("/api/organizations/amortization-plans/", {**terms, "clientId": self.client_id}, format="json")
        self.assertEqual(plan.status_code, 201, plan.data)
        self.generate(self.api, "/api/pdf/schedule/", {"source": "plan", "planId": plan.data["plan"]["id"]})
        self.generate(self.api, "/api/pdf/schedule/", {"source": "plan", "planId": "11111111-1111-1111-1111-111111111111"}, expected=404)

    def test_schedule_requests_are_validated(self):
        self.generate(self.api, "/api/pdf/schedule/", {"source": "recorded"}, expected=400)
        self.generate(self.api, "/api/pdf/schedule/", {"source": "plan"}, expected=400)
        self.generate(self.api, "/api/pdf/schedule/", {"source": "simulation", "terms": {"loanType": "multi"}}, expected=400)
        self.generate(self.api, "/api/pdf/schedule/", {"source": "recorded", "reference": "PNG-9999"}, expected=404)

    def test_personal_accounts_cannot_generate_schedules_or_fiscal_documents(self):
        _, personal = self.user_api("solo@example.com")
        self.generate(personal, "/api/pdf/schedule/", {"source": "recorded", "reference": self.reference}, expected=404)
        self.generate(personal, "/api/pdf/fiscal/", {"type": "invoice", "number": 1}, expected=404)


class PdfStorageAndCleanupTests(PdfTestCase):
    def setUp(self):
        super().setUp()
        self.user, _ = self.user_api("store@example.com")
        self.other, _ = self.user_api("store2@example.com")

    def age(self, path, seconds):
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_stored_files_are_found_only_by_their_owner(self):
        stored = pdf_store.store_pdf(self.user, "pingo invoice/ä.pdf", b"%PDF-1.4 test")
        path, name = pdf_store.find_pdf(self.user, stored["id"])
        self.assertEqual(name.endswith(".pdf"), True)
        self.assertEqual(path.read_bytes(), b"%PDF-1.4 test")
        self.assertEqual(path.parent.name, str(self.user.pk))
        self.assertIsNone(pdf_store.find_pdf(self.other, stored["id"]))
        self.assertIsNone(pdf_store.find_pdf(self.user, "not-a-uuid"))
        self.assertIsNone(pdf_store.find_pdf(self.user, "../../etc/passwd"))
        self.assertEqual(list(Path(self.storage.name).rglob("*.part")), [])

    def test_only_files_older_than_the_limit_are_deleted(self):
        old = pdf_store.store_pdf(self.user, "old.pdf", b"old")
        fresh = pdf_store.store_pdf(self.user, "fresh.pdf", b"fresh")
        lone = pdf_store.store_pdf(self.other, "lone.pdf", b"lone")
        self.age(pdf_store.find_pdf(self.user, old["id"])[0], 301)
        self.age(pdf_store.find_pdf(self.other, lone["id"])[0], 3600)
        self.assertEqual(pdf_store.delete_expired(max_age_seconds=300), 2)
        self.assertIsNone(pdf_store.find_pdf(self.user, old["id"]))
        self.assertIsNotNone(pdf_store.find_pdf(self.user, fresh["id"]))
        self.assertFalse((Path(self.storage.name) / str(self.other.pk)).exists(), "an emptied user folder is removed")

    def test_abandoned_partial_writes_are_swept_too(self):
        folder = Path(self.storage.name) / str(self.user.pk)
        folder.mkdir(parents=True)
        partial = folder / "abc__x.pdf.part"
        partial.write_bytes(b"half")
        self.age(partial, 1000)
        self.assertEqual(pdf_store.delete_expired(max_age_seconds=300), 1)

    @override_settings(GENERATED_PDF_TTL_SECONDS=120)
    def test_the_cleanup_command_uses_the_configured_age_and_can_be_overridden(self):
        stored = pdf_store.store_pdf(self.user, "a.pdf", b"a")
        self.age(pdf_store.find_pdf(self.user, stored["id"])[0], 130)
        out = StringIO()
        call_command("cleanup_generated_pdfs", stdout=out)
        self.assertIn("Deleted 1 generated PDF(s).", out.getvalue())
        kept = pdf_store.store_pdf(self.user, "b.pdf", b"b")
        out = StringIO()
        call_command("cleanup_generated_pdfs", "--max-age", "0", stdout=out)
        self.assertIn("Deleted 1 generated PDF(s).", out.getvalue())
        self.assertIsNone(pdf_store.find_pdf(self.user, kept["id"]))

    def test_an_expired_download_answers_with_a_clear_404(self):
        _, api = self.user_api("late@example.com")
        reference, _ = self.make_debt(api)
        response = self.generate(api, "/api/pdf/debt/", {"reference": reference})
        self.age(pdf_store.find_pdf(User.objects.get(email="late@example.com"), response.data["id"])[0], 4000)
        pdf_store.delete_expired(max_age_seconds=300)
        gone = self.fetch(api, response.data["url"])
        self.assertEqual(gone.status_code, 404)
        self.assertIn("Generate it again", gone.data["detail"])
