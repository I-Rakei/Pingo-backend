"""Wording and number/date formatting for generated PDF documents, in English and Mozambican Portuguese.

The wording mirrors the web app's `pdf` dictionary. Portuguese uses the pre-AO90 spelling
(Factura, Extracto, projecção) and the pt-MZ conventions: dd/mm/yyyy dates and a comma decimal
separator, with a space between thousands only from five digits (3033,33 but 12 345,67).
"""

from decimal import Decimal

from .services import money

LANGUAGES = ("en", "pt")
MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec")


def normalize_language(language):
    return "pt" if language == "pt" else "en"


def format_number(value, language):
    """Two decimals, with the language's separators."""
    amount = money(Decimal(value or 0))
    sign = "-" if amount < 0 else ""
    whole, fraction = f"{abs(amount):.2f}".split(".")
    if normalize_language(language) == "pt":
        grouped = f"{int(whole):,}".replace(",", " ") if len(whole) >= 5 else whole
        return f"{sign}{grouped},{fraction}"
    return f"{sign}{int(whole):,}.{fraction}"


def format_money(value, currency, language):
    return f"{format_number(value, language)} {currency}"


def format_date(value, language):
    if not value:
        return "-"
    if normalize_language(language) == "pt":
        return f"{value.day:02d}/{value.month:02d}/{value.year}"
    return f"{value.day:02d} {MONTHS_EN[value.month - 1]} {value.year}"


EN = {
    "invoice": "Invoice", "receipt": "Receipt", "document": "Document",
    "fiscalTitles": {"invoice": "Invoice", "debit_note": "Debit note", "credit_note": "Credit note", "balance_note": "Balance note"},
    "paid": "Paid in full", "overdue": "Overdue", "unpaid": "Outstanding", "due": "Due",
    "number": "Number", "issueDate": "Issue date", "paymentDate": "Payment date", "payBy": "Pay by", "loanReference": "Loan reference",
    "receivedFrom": "Received from", "billedTo": "Billed to", "loanDetails": "Loan details", "loanType": "Loan type",
    "single": "Single loan", "multi": lambda months: f"Multi-period, {months} months", "interestRate": "Interest rate",
    "perPeriod": "per period", "status": "Status",
    "instalment": lambda number: f"Instalment {number}", "interestPeriod": lambda number: f"Interest, period {number}",
    "capital": "Capital", "interest": "Interest", "capitalRepayment": "Capital repayment",
    "instalmentPeriod": lambda number: f"Instalment, period {number}", "instalmentPayment": "Instalment payment",
    "periodPayment": lambda number: f"Payment for period {number}", "capitalPayment": "Capital payment",
    "interestPayment": "Interest payment", "payment": "Payment",
    "description": "Description", "dueDate": "Due date", "amount": "Amount", "capitalDue": "Capital due",
    "interestDue": "Interest due", "totalDue": "Total due",
    "amountsDue": "Amounts due",
    "paymentInstructions": lambda date: f"Please pay the amounts above by their due dates. First payment due {date}.",
    "nothingDue": "Nothing is due. This loan is paid in full.", "amountReceived": "Amount received", "stillOwed": "Still owed",
    "paymentReceived": "Payment received", "paymentsReceived": "Payments received", "date": "Date",
    "settled": "Paid in full. Nothing further is due on this loan.",
    "creditedTo": "Credited to", "documentDetails": "Document details", "issuedBy": "Issued by",
    "balanceStatement": "Outstanding balance statement",
    "statement": "Statement", "details": "Details", "reason": "Reason", "balanceOutstanding": "Balance outstanding", "total": "Total",
    "amortization": "Amortization plan", "simulation": "SIMULATION", "prepared": "Prepared", "startDate": "Start date",
    "finalDue": "Final due",
    "preparedFor": "Prepared for", "client": "Client", "prospectiveClient": "Prospective client", "creditTerms": "Credit terms",
    "principal": "Principal",
    "flatTerm": "flat for the term", "repayment": "Repayment", "interestOnly": "Interest-only + capital",
    "equalPeriods": "Equal periods",
    "paymentPlan": lambda currency: f"Payment plan ({currency})", "penalty": "Penalty", "capitalLeft": "Capital left",
    "penalties": "Penalties", "scheduledTotal": "Scheduled total",
    "simulationFooter": "Simulation only; not a credit contract.", "scheduleFooter": "Ledger schedule only; not a credit contract.",
    "projectionNote": "Projection only. No loan was created. This is not a credit contract or payment receipt. Penalties are excluded.",
    "ledgerNote": lambda collected, outstanding: f"Ledger figures at preparation: {collected} collected; {outstanding} outstanding.",
    "scheduleExplanation": "Capital left assumes scheduled payments are made. Interest-only loans settle capital separately. "
                           "Equal payments are rounded to whole units, with the last payment taking the remainder.",
    "footerNote": "Computer-generated document. Valid without signature.",
    "generated": lambda date: f"Generated with Pingo on {date}", "page": lambda number, total: f"Page {number} of {total}",
}

PT = {
    "invoice": "Factura", "receipt": "Recibo", "document": "Documento",
    "fiscalTitles": {"invoice": "Factura", "debit_note": "Nota de débito", "credit_note": "Nota de crédito", "balance_note": "Nota de saldo"},
    "paid": "Liquidado", "overdue": "Em atraso", "unpaid": "Por pagar", "due": "A pagar",
    "number": "Número", "issueDate": "Data de emissão", "paymentDate": "Data de pagamento", "payBy": "Pagar até",
    "loanReference": "Referência do empréstimo",
    "receivedFrom": "Recebido de", "billedTo": "Facturado a", "loanDetails": "Dados do empréstimo", "loanType": "Tipo de empréstimo",
    "single": "Empréstimo único", "multi": lambda months: f"Prestações, {months} meses", "interestRate": "Taxa de juro",
    "perPeriod": "por período", "status": "Estado",
    "instalment": lambda number: f"Prestação {number}", "interestPeriod": lambda number: f"Juros, período {number}",
    "capital": "Capital", "interest": "Juros", "capitalRepayment": "Reembolso de capital",
    "instalmentPeriod": lambda number: f"Prestação, período {number}", "instalmentPayment": "Pagamento de prestação",
    "periodPayment": lambda number: f"Pagamento do período {number}", "capitalPayment": "Pagamento de capital",
    "interestPayment": "Pagamento de juros", "payment": "Pagamento",
    "description": "Descrição", "dueDate": "Vencimento", "amount": "Montante", "capitalDue": "Capital a pagar",
    "interestDue": "Juros a pagar", "totalDue": "Total a pagar",
    "amountsDue": "Montantes a pagar",
    "paymentInstructions": lambda date: f"Pague os montantes acima até às respectivas datas. Primeiro pagamento até {date}.",
    "nothingDue": "Nada a pagar. Este empréstimo está liquidado.", "amountReceived": "Montante recebido", "stillOwed": "Saldo por pagar",
    "paymentReceived": "Pagamento recebido", "paymentsReceived": "Pagamentos recebidos", "date": "Data",
    "settled": "Liquidado. Nada mais a pagar por este empréstimo.",
    "creditedTo": "Creditado a", "documentDetails": "Dados do documento", "issuedBy": "Emitido por",
    "balanceStatement": "Extracto do saldo por pagar",
    "statement": "Extracto", "details": "Detalhes", "reason": "Motivo", "balanceOutstanding": "Saldo por pagar", "total": "Total",
    "amortization": "Plano de amortização", "simulation": "SIMULAÇÃO", "prepared": "Preparado em", "startDate": "Data de início",
    "finalDue": "Vencimento final",
    "preparedFor": "Preparado para", "client": "Cliente", "prospectiveClient": "Potencial cliente", "creditTerms": "Condições do crédito",
    "principal": "Capital",
    "flatTerm": "fixa para o prazo", "repayment": "Reembolso", "interestOnly": "Juros + capital separado",
    "equalPeriods": "Prestações iguais",
    "paymentPlan": lambda currency: f"Plano de pagamentos ({currency})", "penalty": "Multa", "capitalLeft": "Capital em dívida",
    "penalties": "Multas", "scheduledTotal": "Total previsto",
    "simulationFooter": "Apenas simulação; não constitui contrato de crédito.",
    "scheduleFooter": "Plano do registo; não constitui contrato de crédito.",
    "projectionNote": "Apenas projecção. Não foi criado nenhum empréstimo. Não constitui contrato de crédito nem recibo de pagamento. Exclui multas.",
    "ledgerNote": lambda collected, outstanding: f"Valores à data de preparação: {collected} recebidos; {outstanding} por pagar.",
    "scheduleExplanation": "O capital em dívida pressupõe o cumprimento dos pagamentos previstos. Empréstimos só de juros liquidam o capital "
                           "separadamente. As prestações iguais são arredondadas a unidades inteiras, e a última fica com o resto.",
    "footerNote": "Documento gerado por computador. Válido sem assinatura.",
    "generated": lambda date: f"Gerado com Pingo em {date}", "page": lambda number, total: f"Página {number} de {total}",
}


def get_copy(language):
    return PT if normalize_language(language) == "pt" else EN
