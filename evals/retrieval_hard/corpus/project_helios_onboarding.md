# Project Helios: Technical Overview and Engineer Onboarding Guide

> **PROJECT DOCUMENTATION: PROJ-HELIOS-2026**
> 
> *Target System: Helios Customer Intelligence Copilot | Current Phase: Pre-Beta (Status: GREEN)*

---

## 1. Executive Summary and Architecture

Project Helios is an intelligent assistant system engineered to perform automated customer interaction telemetry, churn risk forecasting, and conversational feedback synthesis. 

- **Frontend & Integration Tech Lead**: Mei-Ling Zhou (`EMP-4488`, Staff Frontend Engineer)
- **Quality Assurance & Verification Lead**: Dev Patel (`EMP-5104`, Staff QA Engineer)
- **Official Beta Launch Date**: Scheduled for November 12, 2026
- **Primary Source Code Repository**: `git@github.orbit.corp:helios/engine.git`
- **Internal Service Port**: The core service binds locally and listens on HTTPS port `PORT-9443`.

---

## 2. Infrastructure and Data Engine Dependencies

Helios functions as a downstream consumer of enterprise platform services:

1. **Vector Pipeline Sync**: Real-time vector embeddings and semantic search representations are supplied by the central Vector Pipeline service managed by Priyah Sharma (`EMP-2190`) and the Data Engine squad.
2. **Scheduled Replication Window**: Automated document index replication and vector embedding cache flushes are scheduled daily at 03:00 UTC.
3. **Secret Store Path**: Production credentials and API tokens must be fetched exclusively from HashiCorp Vault path `secret/projects/helios/prod-api-key`.

---

## 3. Onboarding Checklist for New Contributors

Engineers joining the Helios team must complete the following onboarding checklist within their initial two weeks:

| Step Number | Task Description | Verification Artifact | Approver |
|---|---|---|---|
| Step 1 | Clone repository and build container image `helios-core:v2` | Docker SHA digest | Dev Patel (`EMP-5104`) |
| Step 2 | Request developer access to port `PORT-9443` on dev gateway | JIRA ticket `HELIOS-DEV-10` | Marcus Vance (`EMP-1823`) |
| Step 3 | Verify ingestion handshake with Data Engine vector sync | Log snippet timestamp | Priyah Sharma (`EMP-2190`) |
| Step 4 | Pass end-to-end integration test suite `npm run test:e2e` | CI pipeline green badge | Mei-Ling Zhou (`EMP-4488`) |
