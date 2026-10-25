# Hardware Inventory, Facilities Directory, and Lab Equipment Rules

> **FACILITIES OPERATING GUIDE: FAC-OPS-2026**
> 
> *Issued by: Corporate Facilities & Hardware Engineering | Scope: Global Labs, Offices, and Datacenters*

---

## 1. Laboratory Facilities and Datacenter Locations

ORBIT operates two primary technological infrastructure facilities for testing and production workloads:

- **North Campus R&D Lab**: Located in Building C, Room 304 (`LAB-304`). Houses experimental robotics rigs, signal analyzers, and hardware debugging benches.
- **South Datacenter Facility DC-2**: Located in the Southern Technology Park (`FAC-DC-2`). Houses production compute clusters, bare-metal nodes, and high-security cryptographic storage vaults.

The dedicated cryptographic hardware security module `HSM-9900` is permanently installed in Server Rack 12-B inside Facility DC-2 (Vault Annex Room 108). Physical access keycards for Vault Annex Room 108 can only be authorized and issued by Facilities Operations Manager Arthur Pendelton (Telephone Extension `EXT-5520`, Security Badge `FAC-881`).

---

## 2. Standard Equipment Allocations

Full-time employees receive standard computing equipment upon onboarding according to functional role:

| Role Category | Primary Laptop Model | Memory & Storage Specs | Peripheral Package |
|---|---|---|---|
| Engineering & Systems | Lenovo ThinkPad P1 Gen 6 | 64GB DDR5 RAM, 2TB NVMe | Dual Dell 27-inch 4K Displays (`U2723QE`) |
| Product & Design | Apple MacBook Pro 16" M3 Max | 64GB Unified Memory, 1TB SSD | Apple Studio Display 27" 5K |
| Business Operations | Lenovo ThinkPad T14s Gen 4 | 32GB LPDDR5, 512GB SSD | Single Dell 27-inch 4K Display (`U2723QE`) |

All deployed corporate machines bear a tamper-evident barcode asset label with prefix `AST-2026-XXXX`.

---

## 3. Hardware Checkout and Lab Borrowing Policies

Engineers borrowing measurement instruments or prototype boards from Lab 304 must register assets in the inventory portal `equip.orbit.corp`:

1. **Maximum Loan Period**: All lab equipment loans are subject to a strict maximum borrowing window of 21 consecutive calendar days.
2. **Overdue Equipment**: Equipment unreturned after 21 days triggers automated notification to the borrower's director under escalation ticket `LAB-OVR-99`.
3. **Offsite Removal**: Removing hardware from campus premises requires an offsite transit permit signed by Arthur Pendelton (`FAC-881`).
