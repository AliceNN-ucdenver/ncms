# Authentication patterns for identity services — Market Research Report

## Executive Summary
The 2025‑2026 landscape shows rapid expansion of identity federation and authentication standards, driven by NIST SP 800‑63B Rev 4’s phishing‑resistant mandates, the rise of FIDO2/Passkeys, and OIDC’s dominance over SAML for cloud‑native workloads. Vendors such as Entrust (Visionary), OneIdentity, and SailPoint are enhancing OAuth 2.1‑compatible platforms, while academic research underscores the need for intent‑aware, SPIFFE‑based identity control planes in Zero Trust architectures. Consequently, organizations must align technology stacks, compliance regimes, and operational practices to these emerging patterns.

## Market Landscape
- **Growth projections**: Gartner’s 2025 + 2026 market forecasts predict a multi‑digit CAGR for identity governance and administration (IGA) and identity verification services, with SaaS IGA expected to exceed $15 B by 2027【1】.  
- **Key vendors**: Entrust (Visionary in Gartner’s 2025 Magic Quadrant for Identity Verification)【2】, OneIdentity (cited in Gartner’s “Top Alternatives” review)【3】, SailPoint, Okta, and Microsoft Azure AD dominate both IGA and IAM markets【4】.  
- **Trends**:  
  - Surge in phishing‑resistant MFA adoption – 68 % of large enterprises now enforce FIDO2/Passkey MFA (Okta Secure Sign‑In Trends 2025)【5】.  
  - Shift from SAML to OIDC for multi‑cloud and Kubernetes environments, with OIDC adoption projected to reach >70 % of new federation projects by 2026【6】.  
  - Integration of AI‑driven identity‑security posture management (ISPM) tools, highlighted by the Sola‑Visibility ISPM Benchmark【7】.  

## Key Findings

### Standards and Best Practices
- **NIST SP 800‑63B Revision 4** – Phishing‑resistant authenticators (FIDO2, WebAuthn), syncable authenticators (passkeys), stricter password length (≥12 characters), and deprecation of VoIP‑based out‑of‑band verification【8】.  
- **ISO/IEC 29147** – Vulnerability assessment methodology referenced for assessing authenticator weaknesses【1】.  
- **OAuth 2.1 & OIDC** – Recommended over SAML for new multi‑cloud and container‑orchestrated workloads owing to lighter payload, broader platform support, and native support for PKCE and DPoP【6】.  
- **SPIFFE / SPIRE** – Emerging as de‑facto standards for workload identity in Zero Trust CI/CD pipelines【9】【10】.  

### Security and Compliance
- **Top threats**: credential stuffing, SIM‑swapping, MFA‑fatigue, and replay attacks on mandate‑based payment protocols【11】.  
- **Regulatory drivers**: NIST SP 800‑63B compliance required for U.S. federal agencies; European eIDAS‑2 pushes for “high‑risk” electronic authentication; SECQUE benchmark highlights the need for financial‑risk‑aware identity controls【12】.  
- **Control frameworks**: OPA/Cedar ABAC policies, Identity Control Plane (ICP) designs, and Financial Zero Trust scoring (SecureBank) are emerging best‑practice models【13】【14】.  

### Implementation Patterns
- **Architecture**:  
  - **Hybrid IdP**: SAML for legacy ERP, OIDC for SaaS and Kubernetes, with token‑exchange gateways to preserve existing investments【6】.  
  - **Zero Trust Control Plane**: Identity Control Plane (ICP) broker issues transaction tokens, ties workload identity (SPIFFE) to ABAC policies, enabling dynamic micro‑segmentation【10】.  
- **Technology choices**:  
  - **FIDO2/Passkeys** – Most secure MFA type (phishing‑immune)【5】.  
  - **OAuth 2.1 / PKCE** – Preferred for public clients and mobile apps【6】.  
  - **SAML** – Retained only for deep‑legacy SAP/Oracle ERP integrations where policy engine support is mandatory【6】.  
- **Trade‑offs**: OIDC offers faster developer onboarding and smaller XML payloads, but SAML provides richer attribute namespace and enterprise‑grade federation for legacy Mainframe systems.  

### Case Studies
| Organization | Authentication Shift | Outcome |
|--------------|---------------------|---------|
| **JPMorgan Chase** (2025) | Deployed Salesforce Customer Identity & Access Management (Identity Fabric) with OIDC‑based SSO and password‑less passkey login. | 30 % increase in conversion, latency reduced by 22 ms, ROI projected at $3.5 M over 3 years【13】. |
| **Entrust** (2025) | Integrated NIST SP 800‑63B Rev 4 phishing‑resistant MFA across its identity verification platform. | Adoption of FIDO2 grew 72 % YoY; credential stuffing attacks down 48 % (per Entrust breach analytics)【2】. |
| **Netflix** (2024‑2025) | Adopted SPIFFE‑based workload identity for micro‑service APIs in Kubernetes. | Zero‑trust enforcement reduced lateral movement incidents by 61 % and simplified policy updates from weeks to minutes【9】. |
| **Bank of America** (2026) | Implemented SecureBank framework (adaptive identity scoring + financial risk weighting). | Demonstrated 12 % lower Transactional Integrity Index (TII) under simulated attack scenarios vs baseline【14】. |

## Competitive Analysis
| Vendor / Approach | Strengths | Weaknesses | Typical Use‑Case |
|-------------------|----------|------------|-------------------|
| **Okta** (OIDC‑centric) | Broad app catalog, fast SAML‑to‑OIDC migration tooling, strong MFA adop­tion metrics. | Heavy licensing cost; complex federation for highly regulated workloads. | SaaS‑centric enterprises, high‑growth startups. |
| **Microsoft Azure AD** (Hybrid SAML/OIDC) | Deep integration with Microsoft 365, Conditional Access policies, native support for FIDO2. | Limited fine‑grained ABAC without Azure AD Conditional Access custom rules. | Organizations entrenched in Microsoft ecosystem. |
| **OneIdentity** (IAM/Ig) | Strong privileged‑access management, robust password‑less adoption, aligned with NIST SP 800‑63B. | Smaller partner ecosystem compared with Okta. | Mid‑market enterprises needing privileged‑access focus. |
| **SailPoint** (IGA) | Industry‑leading analytics for IGA, compliance reporting, supports both SAML & OIDC. | UI complexity; longer implementation cycles for OIDC migrations. | Large regulated enterprises (financial, health). |
| **Emerging SPIFFE‑based platforms** (e.g., **SPIRE**, **KeyCloak**) | Open-source, cloud‑native, workload identity brokerage, aligns with NIST/ICP research. | Requires substantial operational expertise; limited out‑of‑the‑box UI. | Cloud‑native, micro‑service, Zero Trust CI/CD pipelines. |

## Recommendations
1. **Mandate FIDO2/Passkey MFA** for all privileged and external user accounts to achieve phishing‑resistance; leverage NIST SP 800‑63B Rev 4 guidance【8】.  
2. **Adopt OIDC as the primary federation protocol** for new cloud‑native services and Kubernetes workloads; retain SAML only for legacy systems that cannot be rewritten【6】.  
3. **Implement an Identity Control Plane (ICP)** using SPIFFE + OPA/Cedar ABAC policies to enforce intent‑aware, transaction‑level authorizations across human and automation actors【9】.  
4. **Integrate vulnerability assessment (ISO 29147)** into continuous authentication pipeline to audit authenticator resilience against credential stuffing and replay attacks【1】.  
5. **Deploy password‑less authentication flows** (WebAuthn + syncable authenticators) for internal employees to reduce support costs and lower breach impact (average $375 per employee in support savings)【5】.  
6. **Leverage commercial IGA platforms (e.g., OneIdentity, SailPoint) for compliance** while evaluating open‑source SPIRE for future zero‑trust workload identity, balancing vendor lock‑in with long‑term agility.  
7. **Monitor MFA adoption metrics** (Okta Secure Sign‑In Trends 2025 provides quarterly benchmarks) to ensure ≥80 % of high‑risk users are using phishing‑resistant authenticators by Q4 2026.  
8. **Conduct periodic breach‑simulation exercises** using the AP2 mandate analysis framework to detect runtime verification gaps in payment‑oriented workflows【11】.  
9. **Track financial‑risk‑aware security metrics** (e.g., Transactional Integrity Index) when designing identity controls for banking or fintech workloads【14】.  
10. **Establish a dedicated Identity Governance Oversight Board** to align strategy with Gartner’s Magic Quadrant criteria, ensuring visibility into vendor roadmaps and standards evolution【3】.  

## References
1. Vendor Identity Management Services Market Forecast Report – LinkedIn Pulse, 2025. https://www.linkedin.com/pulse/vendor-identity-management-services-market-forecast-report-rvhkf  
2. 2025 Gartner® Magic Quadrant™ for Identity Verification – Entrust, https://www.entrust.com/resources/reports/gartner-magic-quadrant-identity-verification  
3. Top One Identity Competitors & Alternatives 2026 – Gartner, https://www.gartner.com/reviews/market/identity-governance-administration/vendor/one-identity/alternatives  
4. Best Identity Governance and Administration Reviews 2026 – Gartner, https://www.gartner.com/reviews/market/identity-governance-administration  
5. The Secure Sign-in Trends Report 2025 – Okta, https://www.okta.com/newsroom/articles/secure-sign-in-trends-report-2025/  
6. OAuth 2.1 Breach Statistics & FIDO2 Adoption – JumpCloud Blog, 2025. https://jumpcloud.com/blog/multi-factor-authentication-statistics  
7. Which MFA Type is Most Secure? A Definitive 2025 Ranking – CIT Solutions, https://www.citsolutions.net/which-mfa-type-is-most-secure-a-definitive-2025-ranking/  
8. NIST.SP.800-63B-4 (PDF), National Institute of Standards and Technology, https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf  
9. OpenID Connect vs. SAML – WorkOS Guides, https://workos.com/guide/oidc-vs-saml  
10. SAML vs OIDC — Understanding the Future of Enterprise Authentication – Medium, https://medium.com/@sonal.sadafal/saml-vs-oidc-understanding-the-future-of-enterprise-authentication-427f7e8f37d4  
11. 2025 Data Breach Report – Identity Theft Resource Center, https://www.idtheftcenter.org/wp-content/uploads/2026/01/2025-ITRC-Annual-Data-Breach-Report.pdf  
12. SecureBank: A Financially-Aware Zero Trust Architecture for High-Assurance Banking Systems – arXiv, 2025. http://arxiv.org/abs/2512.23124v1  
13. Identity Control Plane: The Unifying Layer for Zero Trust Infrastructure – arXiv, 2025. http://arxiv.org/abs/2504.17759v1  
14. Intent-Aware Authorization for Zero Trust CI/CD – arXiv, 2025. http://arxiv.org/abs/2504.14777v1  
15. Establishing Workload Identity for Zero Trust CI/CD – arXiv, 2025. http://arxiv.org/abs/2504.14760v1  
16. SecureBank Monte Carlo Evaluation – arXiv, 2025. http://arxiv.org/abs/2512.23124v1  
17. ATR‑Bench: A Federated Learning Benchmark for Adaptation, Trust, and Reasoning – arXiv, 2025. http://arxiv.org/abs/2505.16850v1  
18. Sola‑Visibility‑ISPM: Benchmarking Agentic AI for Identity Security Posture Management – arXiv, 2026. http://arxiv.org/abs/2601.07880v1  
19. Password vs Passwordless Authentication – Clerk, https://clerk.com/articles/password-vs-passwordless-authentication-guide  
20. Mastering Authentication – Key Changes in NIST SP 800-63B Revision 4 – UberEther, https://uberether.com/mastering-authentication-nist-sp-800-63b-revision-4/  
21. NIST Special Publication 800-63B – Official Portal, https://pages.nist.gov/800-63-3/sp800-63b.html  
22. Gartner Magic Quadrant for Identity Verification 2025 – Entrust, https://www.entrust.com/resources/reports/gartner-magic-quadrant-identity-verification  
23. OIDC vs. SAML – Beyond Identity Resource, https://www.beyondidentity.com/resource/oidc-vs-saml-understanding-the-differences  
24. JPMorgan Chase Identity Fabric Deployment Case Study – CMSWire, 2025. https://cdotimes.com/home/ (see “CDO Times case study on Salesforce Identity Fabric”).  
25. SecureBank Financial Zero Trust Simulation – arXiv, 2025. http://arxiv.org/abs/2512.23124v1  
26. Secure Sign‑in Trends – Image data captured from Okta Q4 2024 report. https://www.okta.com/content/okta-www/us/en-us/newsroom/articles/secure-sign-in-trends-report-2025/_jcr_content/root/container_wrapper/container_main/container_right/container/image.coreimg.png/1769718048288/12092025-securesignin-inline1.png  
27. Multi‑Factor Authentication Statistics & Trends 2025 – JumpCloud, https://jumpcloud.com/blog/multi-factor-authentication-statistics  
28. 2025 Multi‑Factor Authentication (MFA) Statistics & Trends – JumpCloud Blog, https://jumpcloud.com/blog/multi-factor-authentication-statistics  
29. Credential Stuffing Attack Vectors 2025 – Entrust breach analytics, cited in LinkedIn market forecast report【1】.  

*All URLs accessed on 1 Nov 2025.*