"""
Verbose debug script for Payment Entry CFDI generation
Run with: bench execute --verbose mexico_einvoice.debug_payment
"""

import frappe
import json
import requests

def debug_payment_cfdi():
    """Debug Payment Entry CFDI generation with verbose output"""
    
    pe_name = "ACC-PAY-2026-00011"
    
    print("=" * 60)
    print("PAYMENT ENTRY CFDI DEBUG SCRIPT")
    print("=" * 60)
    
    # Get Payment Entry
    pe = frappe.get_doc("Payment Entry", pe_name)
    print(f"\n1. Payment Entry: {pe.name}")
    print(f"   Party (Customer): {pe.party}")
    print(f"   Party Type: {pe.party_type}")
    print(f"   Payment Form: {pe.payment_form}")
    
    # Get customer details
    print("\n2. Getting customer details...")
    customer_name, tax_id, tax_system = frappe.db.get_value(
        "Customer", pe.party, ["customer_name", "tax_id", "tax_system"]
    )
    print(f"   Customer Name: {customer_name}")
    print(f"   Tax ID: {tax_id}")
    print(f"   Tax System: {tax_system}")
    
    # Check if foreign
    is_foreign = False
    if tax_id:
        import re
        if tax_id.upper().startswith(("XAXX", "XEXX", "XEXT")):
            is_foreign = True
            print(f"   Detected as FOREIGN (generic RFC)")
        elif not re.match(r'^[A-Z&Ñ]{3,4}[0-9]{6}[A-Z0-9]{2,3}$', tax_id.upper()):
            is_foreign = True
            print(f"   Detected as FOREIGN (non-Mexican pattern)")
        else:
            print(f"   Detected as MEXICAN")
    
    # Get address
    print("\n3. Getting address...")
    query = (
        f'select email_id, pincode, country from `tabAddress` where name in '
        f'(select customer_primary_address from `tabCustomer` where name = "{pe.party}")'
    )
    address = frappe.db.sql(query, as_dict=1)
    
    if not address:
        print("   ERROR: No address found!")
        return
    
    print(f"   Email: {address[0]['email_id']}")
    print(f"   Pincode: {address[0]['pincode']}")
    print(f"   Country: {address[0].get('country', 'None')}")
    
    # Process postal code
    zip_code = address[0]["pincode"]
    country = address[0].get("country", "Mexico")
    
    print(f"\n4. Processing postal code...")
    print(f"   Original postal code: {zip_code}")
    print(f"   Original country: {country}")
    print(f"   Is foreign: {is_foreign}")
    
    if is_foreign:
        if not zip_code or zip_code == "19007":
            zip_code = "00000"
            print(f"   -> Changed to: {zip_code} (foreign default)")
        
        country_2to3 = {
            "MEXICO": "MEX", "USA": "USA", "UNITED STATES": "USA",
            "CANADA": "CAN", "UNITED KINGDOM": "GBR",
            "GERMANY": "DEU", "FRANCE": "FRA", "SPAIN": "ESP",
        }
        country = country_2to3.get(country.upper(), "USA") if country else "USA"
        print(f"   -> Country converted to: {country}")
    
    # Build customer object
    customer = {
        "legal_name": customer_name,
        "email": address[0]["email_id"],
        "tax_id": tax_id,
        "tax_system": tax_system,
    }
    
    if is_foreign:
        customer["address"] = {"zip": zip_code, "country": country}
    else:
        customer["address"] = {"zip": zip_code}
    
    print(f"\n5. Customer object being sent to API:")
    print(json.dumps(customer, indent=2))
    
    # Build related documents
    print("\n6. Building related documents...")
    related_documents = []
    for rel_doc in pe.references:
        if rel_doc.reference_doctype == "Sales Invoice":
            uuid = frappe.get_value("Sales Invoice", rel_doc.reference_name, "uuid")
            print(f"   Sales Invoice: {rel_doc.reference_name}")
            print(f"   UUID: {uuid}")
            
            # Get taxes
            si_doc = frappe.get_doc("Sales Invoice", rel_doc.reference_name)
            taxes = []
            if si_doc.taxes_and_charges:
                for tax in pe.taxes:
                    tax_rate = tax.rate or 0
                    taxes.append({
                        "base": rel_doc.allocated_amount / (1 + (tax_rate / 100)) if tax_rate else rel_doc.allocated_amount,
                        "type": "IVA",
                        "rate": tax_rate / 100,
                    })
            else:
                taxes.append({"base": rel_doc.allocated_amount / (1 + 0.16), "type": "IVA", "rate": 0.16})
            
            invoice_details = {
                "uuid": uuid,
                "amount": rel_doc.allocated_amount,
                "last_balance": rel_doc.outstanding_amount,
                "installment": 1,  # Simplified
                "taxes": taxes,
            }
            related_documents.append(invoice_details)
    
    # Build complete payload
    complements = [
        {
            "type": "pago",
            "data": [{"payment_form": pe.payment_form, "related_documents": related_documents}],
        }
    ]
    
    data = {"type": "P", "customer": customer, "complements": complements}
    
    print("\n7. COMPLETE API PAYLOAD:")
    print(json.dumps(data, indent=2))
    
    # Make API call
    print("\n8. Making API call to Facturapi...")
    from mexico_einvoice.utils import get_token
    
    token = get_token()
    url = "https://www.facturapi.io/v2/invoices"
    header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    json_data = json.dumps(data)
    print(f"   URL: {url}")
    print(f"   Headers: {header}")
    
    response = requests.post(url, headers=header, data=json_data)
    print(f"   Response Status: {response.status_code}")
    
    if response.status_code == 200:
        print("\n   ✅ SUCCESS!")
        print(json.dumps(response.json(), indent=2))
    else:
        print("\n   ❌ ERROR:")
        print(json.dumps(response.json(), indent=2))

# Run the debug
debug_payment_cfdi()