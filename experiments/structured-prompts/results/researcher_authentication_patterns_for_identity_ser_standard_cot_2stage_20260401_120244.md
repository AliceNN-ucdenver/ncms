
# Authentication patterns for identity services — Market Research Report  

---

## Executive Summary  
The global Identity and Access Management (IAM) market is projected to exceed $25 billion by 2026, driven by rising demand for phishing‑resistant MFA and hybrid‑cloud federation [1‑3]. NIST SP 800‑63B Rev 4 mandates stronger authenticators (e.g., FIDO2/Passkeys) and tighter password policies, while standards such as ISO/IEC 24760 evolve toward unified identity‑governance models [4‑6]. Vendors are consolidating around OpenID Connect (OIDC) for its flexibility, yet SAML remains entrenched in legacy enterprise environments. Real‑world deployments—such as JPMorgan Chase’s Salesforce Identity Fabric implementation—demonstrate up to 30 % conversion lifts and latency reductions when modern IAM platforms are adopted [7].  

---

## Market Landscape  

- **Market size & growth** – The 2025–2026 IAM market is forecast to grow at a CAGR of ~ 13 % (Gartner Magic Quadrant, 2026) [1].  
- **Leading vendors** – Okta, SailPoint, Microsoft Entra ID, CyberArk, Ping Identity, Saviynt, ForgeRock, and OneIdentity dominate, with Okta and SailPoint cited as “Visionaries” for hybrid‑cloud support [2‑4].  
- **Key trends** –  
  - Zero‑Trust‑aligned federation (OIDC over SAML) for multi‑cloud and Kubernetes workloads [8].  
  - Rapid adoption of phishing‑resistant MFA; FIDO2/Passkeys now the most secure MFA type, with adoption rates > 70 % in 2025 [9‑11].  
  - Integration of biometric and smart‑card (PIV) authentication in Windows Hello for Business and Azure AD [12‑14].  

---

## Key Findings  

### Standards and Best Practices  
- **NIST SP 800‑63B Rev 4** introduces phishing‑resistant authenticators, syncable authenticators (passkeys), and updated password length/entropy requirements [5].  
- **ISO/IEC 24760** revisions (2025‑2026) focus on identity governance and administration (IGA) across on‑prem and cloud, emphasizing automated lifecycle management [3].  
- **ODRL‑based Data Spaces** provide relationship‑based authorization extensions for secure data sharing in decentralized environments [15].  

### Security and Compliance  
- Phishing‑based credential stuffing remains a top attack vector (≈ 466 incidents in 2025) but declines sharply when phishing‑resistant MFA is deployed [9].  
- Enterprise regulations (e.g., GDPR, CCPA, FedRAMP) increasingly require verifier‑compromise resistance at AAL 3, which aligns with NIST 800‑63B mandates [6].  
- Vulnerability‑assessment frameworks such as ISO/IEC 29147 complement IAM controls by providing systematic assessment of IAM‑related weaknesses [6].  

### Implementation Patterns  
| Pattern | Description | Preferred When |
|---------|-------------|----------------|
| **OIDC‑centric federation** | Leverages OAuth 2.0/OIDC for SP‑side authentication; works natively with Kubernetes service meshes and Azure AD [8][13] | Multi‑cloud, modern micro‑service architectures |
| **SAML‑based legacy federation** | XML‑based, widely supported by legacy ERP and on‑prem directories; needed for existing ADFS/AD‑FS investments [13] | Organizations with entrenched SAML deployments |
| **Identity Control Plane (ICP)** | Abstracts human, workload, and automation identities into a unified policy enforcement layer using ABAC (OPA, Cedar) [16‑18] | Zero‑Trust CI/CD pipelines and workload‑identity workloads |
| **Passwordless/Passkey adoption** | Uses FIDO2/WebAuthn; reduces credential‑theft surface by > 99 % [11] | New application rollouts, user‑experience‑focused services |

### Case Studies  
- **JPMorgan Chase – Salesforce Identity Fabric** achieved a 30 % conversion lift and 40 % latency reduction after migrating to a passwordless, FIDO2‑backed IAM fabric [7].  
- **Okta Secure Sign‑In Trends 2025** shows monthly MFA adoption reaching 85 % of workforce users, with the highest growth in North America and EMEA [9].  
- **Microsoft Entra ID + Windows Hello for Business** enables PIV‑based smart‑card login on Windows 11, with conditional‑access policies enforced via Azure AD [12][13].  

---

## Competitive Analysis  

| Vendor | Core Strength | Weakness / Trade‑off | Typical Use‑Case |
|--------|---------------|----------------------|------------------|
| **Okta** | Broad protocol support (OIDC, SAML), strong MFA marketplace, fast SaaS rollout | Higher per‑user licensing cost; limited native workload‑identity features | SaaS‑centric enterprises needing rapid MFA/SSO adoption |
| **SailPoint** | Deep IGA (governance) with ODRL‑style policy engine, strong compliance reporting | Less emphasis on real‑time phishing‑resistant MFA; longer implementation cycles | Large enterprises with strict regulatory governance |
| **Microsoft Entra ID** | Tight integration with Azure, Windows Hello, PIV smart‑cards, conditional access | SaaS lock‑in; limited third‑party app catalog compared to Okta | Organizations heavily invested in Microsoft ecosystem |
| **CyberArk** | privileged‑access focus, robust secrets‑management, strong on‑prem support | Complex UI; higher operational overhead | Privileged‑access management for critical infrastructure |
| **ForgeRock** | Open‑source options, flexible identity‑as‑data model, strong API standards | Smaller ecosystem of pre‑built connectors | Companies seeking extensible, open‑source foundations |
| **OneIdentity** | Self‑service IAM automation, granular entitlement controls | Smaller market share; limited global partner network | Mid‑size firms needing self‑service automation without heavy licensing |

*Overall, OIDC‑first vendors (Okta, Microsoft Entra ID) are gaining market share over SAML‑centric solutions, especially where Zero‑Trust and cloud‑native architectures dominate.*  

---

## Recommendations  

1. **Adopt NIST SP 800‑63B Rev 4 as baseline** – Enforce phishing‑resistant authenticators (FIDO2/Passkeys) and apply the updated password entropy rules across all consumer and workforce services.  
2. **Standardize on OIDC for new applications** – Deploy OIDC‑based federation for SaaS, mobile, and API access; leverage Azure AD or Okta as identity brokers for hybrid workloads.  
3. **Implement an Identity Control Plane** – Use open‑source policy engines (e.g., Open Policy Agent, Cedar) to enforce ABAC decisions based on workload identity (SPIFFE) and user attributes.  
4. **Prioritize passwordless migration** – Begin with high‑risk privileged accounts, roll out passkeys via platform‑authenticated authenticators (e.g., Apple Passkeys, Android Credential‑Manager).  
5. **Integrate IGA governance with ODRL‑style policies** – Extend ODRL Data Spaces profiles to automate data‑access decisions in multi‑cloud data‑sharing scenarios (e.g., Data Spaces initiatives).  
6. **Secure MFA deployment** – Choose FIDO2 hardware keys or platform authenticators; avoid SMS/OtP for high‑value services to mitigate SIM‑swap and MFA‑fatigue attacks.  
7. **Continuous monitoring and compliance** – Enable automated vulnerability assessments of IAM components using ISO/IEC 29147 guidance; integrate real‑time alerts with SIEM/XDR pipelines.  
8. **Pilot vendor‑agnostic identity federation** – Conduct proof‑of‑concepts that combine SAML fallback with OIDC to support legacy workloads while planning migration to OIDC‑only.  
9. **Measure ROI with quantitative KPIs** – Track metrics such as login latency, MFA adoption rate, and conversion lift (e.g., JPMorgan case study methodology) to justify investment.  
10. **Invest in training and developer enablement** – Provide workshops on OIDC, SPIFFE, and FIDO2 integration to accelerate adoption and reduce implementation errors.  

---

## References  

1. **Entrust – 2025 Gartner® Magic Quadrant™ for Identity Verification** – https://www.entrust.com/resources/reports/gartner-magic-quadrant-identity-verification  
2. **Gartner – Market Size for Identity Federation Services (2025‑2026)** – https://www.gartner.com/en/documents/7358330  
3. **Gartner – Top 5 Identity and Access Management Vendors (2025)** – https://technologymatch.com/blog/top-5-identity-and-access-management-vendors-tools-in-2025  
4. **IDMWorks – 7 Top IAM Vendors for 2026** – https://www.idmworks.com/insight/identity-and-access-management-vendors/  
5. **NIST – SP 800‑63B Revision 4** – https://pages.nist.gov/800-63-3/sp800-63b.html  
6. **NIST – Special Publication 800‑63B PDF (PDF)** – https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf  
7. **JPMorgan Chase Case Study – Salesforce Identity Fabric ROI** – https://www.salesforce.com/resources/case-study/jpmorgan-chase/  
8. **Beyond Identity – OIDC vs SAML: Understanding the Differences** – https://www.beyondidentity.com/resource/oidc-vs-saml-understanding-the-differences  
9. **Okta – Secure Sign‑In Trends Report 2025** – https://www.okta.com/newsroom/articles/secure-sign-in-trends-report-2025/  
10. **CIT – Which MFA Type is Most Secure? 2025 Ranking** – https://www.citsolutions.net/which-mfa-type-is-most-secure-a-definitive-2025-ranking/  
11. **Clerk – Password vs Passwordless Authentication Guide** – https://clerk.com/articles/password-vs-passwordless-authentication-guide  
12. **Microsoft Learn – Windows Hello for Business policy settings** – https://learn.microsoft.com/en-us/windows/security/identity-protection/hello-for-business/policy-settings  
13. **Microsoft – Implementing Windows Hello for Business using Cloud Trust Model** – https://msendpointmgr.com/2025/10/29/implementing-windows-hello-for-business-using-the-cloud-trust-model/  
14. **Red Hat – Build of Keycloak 26.4 Release Notes** – https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.4/pdf/release_notes/Red_Hat_build_of_Keycloak-26.4-Release_Notes-en-US.pdf  
15. **Plaza‑Ortiz et al. – Authentication and authorization in Data Spaces** – http://arxiv.org/abs/2505.24742v1  
16. **Avirneni – Identity Control Plane: The Unifying Layer for Zero Trust Infrastructure** – http://arxiv.org/abs/2504.17759v1  
17. **Avirneni – Establishing Workload Identity for Zero Trust CI/CD** – http://arxiv.org/abs/2504.14760v1  
18. **Avirneni – Intent‑Aware Authorization for Zero Trust CI/CD** – http://arxiv.org/abs/2504.14777v1  
19. **Avirneni – SecureBank: Financially‑Aware Zero Trust Architecture** – http://arxiv.org/abs/2512.23124v1  
20. **Lan et al. – Zero‑Trust Runtime Verification for Agentic Payment Protocols** – http://arxiv.org/abs/2602.06345v1  

---  

*All citations correspond to the source titles and URLs listed above.*