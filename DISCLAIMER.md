# Disclaimer

This disclaimer supplements the [MIT License](LICENSE). It does not replace or
modify the license. If the two documents conflict, the license controls. This
document is general information, not legal, security, compliance, export-control,
or professional advice.

## No warranty or security certification

The software and its outputs are provided **"as is"**, without warranties of any
kind, express or implied. This includes, without limitation, warranties of
merchantability, fitness for a particular purpose, non-infringement, accuracy,
availability, security, privacy, compliance, or suitability for confidential or
NDA-governed work.

CodeTrail contains sandboxing, command filtering, endpoint checks, and regression
tests, but it has not undergone a public product-grade security audit. These
controls reduce specific risks; they do not make an untrusted repository, model,
dependency, machine, or network safe.

## Model and tool output

Model responses, retrieved passages, visual interpretations, binary analysis,
generated patches, and suggested commands may be incomplete, incorrect,
misleading, insecure, or incompatible with the target system. Retrieval scores
and citations are aids, not proof that a conclusion is correct. Users must review
and validate outputs before relying on them, especially for firmware, production,
safety-critical, security-sensitive, regulated, or high-impact systems.

## Confidentiality and network behavior

Local defaults do not by themselves guarantee that data remains on one machine.
The effective boundary also depends on llama-server endpoints, OpenCode providers
and built-in tools, project-level configuration, plugins, proxies, imported files,
and other software in the environment. Users are responsible for inspecting the
effective configuration, access controls, logs, generated caches, and network
paths before processing confidential material.

No guarantee is made that local artifacts, caches, logs, embeddings, session
databases, or backups are free of sensitive or derived content. Users must apply
appropriate retention, access-control, encryption, backup, and deletion policies.

## Third-party rights and compliance

The MIT License covers this project's own software only. It does not grant rights
to third-party models, APIs, dependencies, codebases, datasets, specifications,
firmware, generated artifacts, trademarks, or proprietary material. Those items
may have separate licenses, acceptable-use rules, export restrictions, contracts,
or attribution requirements.

Users are solely responsible for how they operate, modify, deploy, combine,
redistribute, or rely on this software and its outputs, including compliance with
applicable laws, regulations, contracts, licenses, NDAs, platform terms,
model-provider terms, and third-party rights.

The authors and contributors do not encourage, endorse, or provide support for
unlawful or unauthorized use. To the maximum extent permitted by applicable law,
they are not liable for claims, damages, losses, or other liability arising from
the software, its outputs, or its use. See the [MIT License](LICENSE) for the
controlling warranty and liability terms, and [Responsible Use](RESPONSIBLE_USE.md)
for operational guidance.
