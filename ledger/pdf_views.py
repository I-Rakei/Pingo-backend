"""Endpoints that generate PDFs on the server and hand the web app a file to download.

POST /api/pdf/debt/        invoice or receipt for a debt
POST /api/pdf/fiscal/      an issued fiscal document (corporate)
POST /api/pdf/schedule/    an amortization schedule (corporate)
GET  /api/pdf/files/<id>/  the generated file; only the account that generated it can fetch it
"""

from django.http import FileResponse
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import pdf_store
from .models import AmortizationPlan
from .pdf_store import DocumentNotFound
from .serializers import AmortizationSimulationSerializer
from .services import get_membership

# The PDF renderer (and its ReportLab dependency) is imported only when a document is actually requested,
# so a missing or broken PDF library can never stop the rest of the API from starting.
FISCAL_TYPES = ("invoice", "debit_note", "credit_note", "balance_note")


def _documents():
    from . import pdf_documents
    return pdf_documents


LANGUAGE = serializers.ChoiceField(choices=["en", "pt"], required=False)


class DebtDocumentRequest(serializers.Serializer):
    reference = serializers.CharField(max_length=24)
    kind = serializers.ChoiceField(choices=["invoice", "receipt"], required=False)
    paymentId = serializers.UUIDField(required=False)
    installmentNumber = serializers.IntegerField(min_value=1, required=False)
    language = LANGUAGE


class FiscalDocumentRequest(serializers.Serializer):
    type = serializers.ChoiceField(choices=list(FISCAL_TYPES))
    number = serializers.IntegerField(min_value=1)
    language = LANGUAGE


class ScheduleRequest(serializers.Serializer):
    source = serializers.ChoiceField(choices=["recorded", "plan", "simulation"])
    reference = serializers.CharField(max_length=24, required=False)
    planId = serializers.UUIDField(required=False)
    terms = serializers.DictField(required=False)
    language = LANGUAGE

    def validate(self, attrs):
        source = attrs["source"]
        if source == "recorded" and not attrs.get("reference"):
            raise serializers.ValidationError({"reference": "This field is required."})
        if source == "plan" and not attrs.get("planId"):
            raise serializers.ValidationError({"planId": "This field is required."})
        if source == "simulation":
            terms = AmortizationSimulationSerializer(data=attrs.get("terms") or {})
            terms.is_valid(raise_exception=True)
            attrs["terms"] = terms.validated_data
        return attrs


def _respond(user, generated):
    stored = pdf_store.store_pdf(user, generated.filename, generated.data)
    return Response({**stored, "url": f"/api/pdf/files/{stored['id']}/"}, status=status.HTTP_201_CREATED)


def _not_found(error):
    return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)


@api_view(["POST"])
def debt_pdf_view(request):
    serializer = DebtDocumentRequest(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        generated = _documents().debt_document(
            request.user, data["reference"], kind=data.get("kind"), payment_id=data.get("paymentId"),
            installment_number=data.get("installmentNumber"), language=data.get("language"))
    except DocumentNotFound as error:
        return _not_found(error)
    return _respond(request.user, generated)


@api_view(["POST"])
def fiscal_pdf_view(request):
    serializer = FiscalDocumentRequest(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        generated = _documents().fiscal_document(request.user, data["type"], data["number"], language=data.get("language"))
    except DocumentNotFound as error:
        return _not_found(error)
    return _respond(request.user, generated)


@api_view(["POST"])
def schedule_pdf_view(request):
    serializer = ScheduleRequest(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    membership = get_membership(request.user)
    if membership is None:
        return _not_found("Only corporate accounts can use amortization schedules.")
    plan = None
    if data["source"] == "plan":
        plan = (AmortizationPlan.objects.filter(public_id=data["planId"], organization=membership.organization)
                .select_related("client", "organization", "debt").first())
        if plan is None:
            return _not_found("Plan not found.")
    terms = data.get("terms")
    if terms is not None:
        terms = {**terms, "clientName": terms.get("clientName", "")}
    try:
        generated = _documents().schedule_document(
            request.user, data["source"], reference=data.get("reference"), plan=plan, terms=terms, language=data.get("language"))
    except DocumentNotFound as error:
        return _not_found(error)
    return _respond(request.user, generated)


@api_view(["GET"])
def pdf_file_view(request, file_id):
    found = pdf_store.find_pdf(request.user, file_id)
    if found is None:
        return Response({"detail": "This document is no longer available. Generate it again."}, status=status.HTTP_404_NOT_FOUND)
    path, filename = found
    response = FileResponse(open(path, "rb"), content_type="application/pdf", as_attachment=True, filename=filename)
    response["Cache-Control"] = "private, no-store"
    return response
