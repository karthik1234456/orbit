# Facility Physical Security and Access Control Standards

> **POLICY DOCUMENT: POL-SEC-2026**
> 
> *Effective Date: February 1, 2026 | Scope: Global Facilities, Server Rooms, and Corporate Offices*

---

## 1. Overview and Security Objectives

Physical security and identity authentication protocols are designed to safeguard corporate personnel, proprietary assets, and hardware computing infrastructure. Access to all ORBIT corporate buildings requires a valid cryptographic smart badge issued during employee onboarding by the Global Security Operations Center (GSOC).

In the event of an urgent security badge malfunction or facility lockout, employees must contact the Security Operations Dispatch Desk directly at extension `EXT-4100`.

---

## 2. Access Privilege Tiers

Physical access permissions are partitioned into four discrete authorization tiers based on role requirements:

| Access Tier | Clearance Designation | Authorized Areas | Clearance Approver |
|---|---|---|---|
| Tier 1 | General Employee | Main lobby, open office zones, cafeterias, rest areas | HR Operations Lead |
| Tier 2 | Engineering & Labs | Hardware prototyping labs (Lab 304), testing benches | Engineering Director |
| Tier 3 | Datacenter & Infrastructure | South Datacenter Facility DC-2, telecommunications closets | Infrastructure VP |
| Tier 4 | Executive Vault & Annex | Secure cryptographic vault rooms (Room 108), safe rooms | Chief Security Officer |

To request temporary elevation to Tier 3 or Tier 4, staff must submit electronic permit request `SEC-AH-77` signed by a corporate vice president at least 48 hours prior to entry.

---

## 3. Two-Factor Authentication and Smart Tokens

Access to corporate workstations, production networks, and facility airlocks mandates multi-factor authentication (MFA):

1. **Hardware Keys**: All engineering and operations personnel are provisioned with dual hardware tokens, model `YubiKey-5C-NFC`, enrolled under serial tag `KEY-MFA-90`.
2. **Credential Rotation**: User passwords must be rotated every 90 calendar days; production API keys and infrastructure certificates must undergo cryptographic regeneration every 180 days.
3. **Tailgating Enforcement**: Holding access doors open for unbadged individuals is strictly prohibited. Violators face mandatory security retraining under sanction code `SEC-SAN-12`.
4. **Visitor Policy**: All external guests must present government photo identification, sign non-disclosure agreement `NDA-VIS-04`, and remain accompanied by an employee badge holder at all times.
