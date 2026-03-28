import json
import re

import frappe
import requests
from frappe import _


@frappe.whitelist()
def get_token():
    e_invoice_setting = frappe.get_doc("E Invoice Setting", "E Invoice Setting")
    token = e_invoice_setting.get_password(fieldname="secret_key", raise_exception=False)
    return token


def generate_einvoice(doc, method):
    e_invoice_setting = frappe.get_doc("E Invoice Setting", "E Invoice Setting")
    if e_invoice_setting.generate_e_invoice:
        token = get_token()
        url = "https://www.facturapi.io/v2/invoices"
        header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        customer = get_customer_details(doc)
        items = get_items(doc)

        data = {
            "customer": customer,
            "items": items,
            "use": "G01",
            "payment_form": "99",
            "payment_method": "PPD",
        }

        # validate advance payment
        validate_advance_payment(data, doc)

        data = json.dumps(data)
        response = requests.post(url, headers=header, data=data)
        if response.status_code == 200:
            response = response.json()
            doc.e_invoice_id = (response.get("id"),)
            doc.uuid = (response.get("uuid"),)
            doc.sat_signature = (response.get("sat_signature"),)
            doc.signature = (response.get("stamp").get("signature"),)
            doc.invoice_status = (response.get("status"),)
            doc.sat_cert_number = (response.get("stamp").get("sat_cert_number"),)
            doc.cfdi_version = (response.get("cfdi_version"),)
            doc.verification_url = response.get("verification_url")

            validate_partial_payment(doc, response)
        else:
            response = response.json()
            frappe.throw(_(response.get("message")))


def validate_advance_payment(data, doc):
    if doc.outstanding_amount == 0:
        data.update({"payment_form": "30", "payment_method": "PUE"})


def validate_partial_payment(doc, response):
    if (doc.total_advance and doc.outstanding_amount) > 0:
        update_partial_payment(doc, response)


def get_customer_details(doc):
    customer_name, tax_id, tax_system = frappe.db.get_value(
        "Customer", doc.customer, ["customer_name", "tax_id", "tax_system"]
    )

    # Determine if customer is foreign (export) - check for Mexican RFC format
    # Mexican RFC format: 6 chars for individuals, 12 chars for companies (e.g., AAA010101XXX)
    # Also exclude generic RFCs for foreigners: XAXX010101000, XEXX010101000
    is_foreign = False
    if tax_id:
        import re
        # Check if it's a valid Mexican RFC (excluding generic foreign RFCs)
        if re.match(r'^[A-Z&Ñ]{3,4}[0-9]{6}[A-Z0-9]{2,3}$', tax_id.upper()):
            # Also check it's not a generic foreign RFC
            if tax_id.upper().startswith(('XAXX', 'XEXX', 'XIME', 'XAUK')):
                is_foreign = True
        else:
            # Doesn't match Mexican RFC pattern - it's foreign
            is_foreign = True

    # Get the customer's primary billing address
    customer_address = frappe.get_doc("Customer", doc.customer).get("customer_primary_address")
    if not customer_address:
        # Fallback: look for default billing address
        customer_address = frappe.db.get_value(
            "Dynamic Link",
            {"parenttype": "Address", "link_doctype": "Customer", "link_name": doc.customer, "is_primary_address": 1},
            "parent"
        )
    
    # Use the customer's primary address if available, otherwise use doc.customer_address
    address_name = customer_address or doc.customer_address
    
    # Get address details including country
    address_data = frappe.db.sql(
        """
            SELECT email_id, pincode, country
            FROM `tabAddress`
            WHERE name = %s
        """,
        (address_name,),
        as_dict=1,
    )
    
    zip_code = address_data[0]["pincode"]
    country = address_data[0].get("country", "MEX")
    
    # Map common country names to ISO 3166-1 alpha-3 codes
    country_code_map = {
        "Mexico": "MEX",
        "United States": "USA",
        "United States of America": "USA",
        "USA": "USA",
        "Canada": "CAN",
        "United Kingdom": "GBR",
        "Germany": "DEU",
        "France": "FRA",
        "Japan": "JPN",
        "China": "CHN",
        "Spain": "ESP",
    }
    
    # Convert country name to ISO code if needed
    if country in country_code_map:
        country = country_code_map[country]
    elif len(country) == 2:  # Already 2-letter code, convert to 3-letter
        # Common 2-letter to 3-letter mappings
        country_2to3 = {
            "MX": "MEX", "US": "USA", "CA": "CAN", "GB": "GBR",
            "DE": "DEU", "FR": "FRA", "JP": "JPN", "CN": "CHN", "ES": "ESP"
        }
        country = country_2to3.get(country.upper(), "MEX")
    # If it's already 3 letters and valid, keep it
    
    # For foreign/export customers, handle postal code differently
    if is_foreign:
        # For foreign customers, use standard foreign postal code or "00000" for non-Mexican addresses
        if not zip_code or zip_code == "19007":
            zip_code = "00000"  # Standard for foreign addresses
        
        # Ensure country is not MEX for export invoices
        if country == "MEX":
            country = "USA"  # Default to USA for export if not specified
    
    # Debug: print what address we're using
    frappe.flags.einvoice_debug = f"Using address: {address_name}, pincode: {zip_code}, country: {country}, is_foreign: {is_foreign}"
    
    # Build customer dict - only add country field for foreign/export customers
    address_dict = {"zip": zip_code}
    if is_foreign:
        address_dict["country"] = country  # Only for export customers
    
    customer = {
        "legal_name": customer_name,
        "email": address_data[0]["email_id"],
        "tax_id": tax_id,
        "tax_system": tax_system,
        "address": address_dict,
    }
    return customer


def get_items(doc):
    items = []

    for item in doc.items:
        # applying taxes
        taxes = []
        if item.item_tax_template:
            item_tax_doc = frappe.get_doc("Item Tax Template", item.item_tax_template)
            for tax in item_tax_doc.taxes:
                taxes.append({"type": tax.maxico_tax_type, "rate": tax.tax_rate / 100})
        elif doc.taxes_and_charges:
            item_tax_doc = frappe.get_doc("Sales Taxes and Charges Template", doc.taxes_and_charges)
            for tax in item_tax_doc.taxes:
                taxes.append({"type": tax.mexico_tax_type, "rate": tax.rate / 100})

        # Get product_key - prioritize Item master mx_product_service_key over Sales Invoice Item product_key
        product_key = None
        
        # First check Item master for mx_product_service_key (the correct SAT code)
        item_doc = frappe.get_doc("Item", item.item_code)
        if item_doc.mx_product_service_key:
            product_key = item_doc.mx_product_service_key
        elif item_doc.product_key:
            product_key = item_doc.product_key
        # Finally fallback to Sales Invoice Item's product_key
        elif item.product_key:
            product_key = item.product_key
        
        # Convert to integer for Facturapi API (must be number, not string)
        if product_key:
            try:
                product_key = int(product_key)
            except (ValueError, TypeError):
                # Keep as string if conversion fails
                pass

        items.append(
            {
                "quantity": item.qty,
                "product": {
                    "description": re.sub("<[^<]+?>", "", _(f"{item.description}")),
                    "product_key": product_key,
                    "price": item.rate,
                    "tax_included": False,
                    "taxes": taxes,
                },
            }
        )
    return items


# def cancel_einvoice(invoice_name, e_invoice_id, motive):
@frappe.whitelist()
def cancel_einvoice(
    invoice_name: str,
    e_invoice_id: str,
    motive: str,
):
    e_invoice_setting = frappe.get_doc("E Invoice Setting", "E Invoice Setting")
    if e_invoice_setting.cancel_e_invoice:
        token = get_token()
        url = "https://www.facturapi.io/v2/invoices/" + e_invoice_id + "?motive=" + motive
        header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = requests.delete(url, headers=header)
        if response.status_code == 200:
            doc = frappe.get_doc("Sales Invoice", invoice_name)
            doc.cancel()
            response = response.json()

            # set reason
            if motive == "01":
                reason = "01 - Receipt issued with errors related to."
            elif motive == "02":
                reason = "02 - Receipt issued with unrelated errors."
            elif motive == "03":
                reason = "03 - The operation was not carried out."
            elif motive == "04":
                reason = "04 - Nominative operation related to the global invoice."

            frappe.db.set_value(
                "Sales Invoice",
                invoice_name,
                {"invoice_status": response.get("status"), "motive": reason},
            )
            # frappe.db.commit()
            return "success"
        else:
            response = response.json()
            return "fail"


def get_customer_from_payment(doc):
    customer_name, tax_id, tax_system = frappe.db.get_value(
        "Customer", doc.party, ["customer_name", "tax_id", "tax_system"]
    )

    query = (
        f"select email_id, pincode from `tabAddress` where name in "
        f'(select customer_primary_address from `tabCustomer` where name = "{doc.party}")'
    )
    address = frappe.db.sql(query, as_dict=1)

    if not address:
        frappe.throw(_("Please, update Customer Primary Address in customer Doctype"))

    customer = {
        "legal_name": customer_name,
        "email": address[0]["email_id"],
        "tax_id": tax_id,
        "tax_system": tax_system,
        "address": {"zip": address[0]["pincode"]},
    }
    return customer


def update_payment(doc, method):
    if not doc.references:
        return

    customer = get_customer_from_payment(doc)

    # update related documents
    related_documents = []
    for rel_doc in doc.references:
        if rel_doc.reference_doctype == "Sales Invoice":
            uuid = frappe.get_value("Sales Invoice", rel_doc.reference_name, "uuid")
            installments = linked_sales_invoice(rel_doc.reference_name)

            # update taxes
            si_doc = frappe.get_doc("Sales Invoice", rel_doc.reference_name)
            taxes = []
            if si_doc.taxes_and_charges:
                for tax in doc.taxes:
                    # Handle None tax.rate - default to 0
                    tax_rate = tax.rate or 0
                    taxes.append(
                        {
                            "base": rel_doc.allocated_amount / (1 + (tax_rate / 100)) if tax_rate else rel_doc.allocated_amount,
                            "type": "IVA",
                            "rate": tax_rate / 100,
                        }
                    )
            else:
                taxes.append(
                    {"base": rel_doc.allocated_amount / (1 + 0.16), "type": "IVA", "rate": 0.16}
                )

            invoice_details = {
                "uuid": uuid,
                "amount": rel_doc.allocated_amount,
                "last_balance": rel_doc.outstanding_amount,
                "installment": installments + 1,
                "taxes": taxes,
            }
            related_documents.append(invoice_details)

    complements = [
        {
            "type": "pago",
            "data": [{"payment_form": doc.payment_form, "related_documents": related_documents}],
        }
    ]

    data = {"type": "P", "customer": customer, "complements": complements}

    token = get_token()
    url = "https://www.facturapi.io/v2/invoices"
    header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    data = json.dumps(data)
    response = requests.post(url, headers=header, data=data)
    if response.status_code == 200:
        response = response.json()

        # updating invoice payments
        for ref in doc.references:
            if ref.reference_doctype == "Sales Invoice":
                si_doc = frappe.get_doc("Sales Invoice", ref.reference_name)
                update_einvoice_payments(si_doc, response)
                si_doc.save()
    else:
        response = response.json()
        frappe.throw(_(response.get("message")))


def linked_sales_invoice(sales_invoice):
    doc = frappe.get_doc("Sales Invoice", sales_invoice)
    installments = len(doc.e_invoice_payments)
    return installments
    # query = (
    #     "select COUNT(name) as installment from `tabPayment Entry Reference` "
    #     "where parenttype='Payment Entry' and reference_doctype='Sales Invoice' "
    #     f"and reference_name='{sales_invoice}'"
    # )
    # count = frappe.db.sql(query, pluck='installment')
    # return count[0]


def update_partial_payment(doc, response):
    customer = get_customer_details(doc)

    uuid = response.get("uuid")

    # update taxes
    taxes = []
    if doc.taxes_and_charges:
        for tax in doc.taxes:
            taxes.append(
                {
                    "base": doc.total_advance / (1 + (tax.rate / 100)),
                    "type": "IVA",
                    "rate": tax.rate / 100,
                }
            )
    else:
        taxes.append({"base": doc.total_advance / (1 + 0.16), "type": "IVA", "rate": 0.16})

    # update related documents
    related_documents = []
    # installments = linked_sales_invoice(doc.name)
    installments = len(doc.e_invoice_payments)
    invoice_details = {
        "uuid": uuid,
        "amount": doc.total_advance,
        "last_balance": doc.grand_total,
        "installment": installments + 1,
        "taxes": taxes,
    }
    related_documents.append(invoice_details)
    complements = [
        {
            "type": "pago",
            "data": [{"payment_form": str(30), "related_documents": related_documents}],
        }
    ]
    data = {"type": "P", "customer": customer, "complements": complements}

    token = get_token()
    url = "https://www.facturapi.io/v2/invoices"
    header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    data = json.dumps(data)
    response = requests.post(url, headers=header, data=data)
    if response.status_code == 200:
        response = response.json()

        # update E-Invoice Payments
        update_einvoice_payments(doc, response)
    else:
        response = response.json()
        frappe.throw(_(response.get("message")))


def update_einvoice_payments(doc, response):
    doc.append(
        "e_invoice_payments",
        dict(
            id=response.get("id"),
            uuid=response.get("uuid"),
            date=response.get("sat_signature"),
            verification_url=response.get("verification_url"),
            status=response.get("status"),
            folio_number=response.get("folio_number"),
            sat_signature=response.get("stamp").get("sat_signature"),
            sat_cert_number=response.get("stamp").get("sat_cert_number"),
            signature=response.get("stamp").get("signature"),
            complement_string=response.get("stamp").get("complement_string"),
        ),
    )
