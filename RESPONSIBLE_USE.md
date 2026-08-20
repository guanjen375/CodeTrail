# Responsible Use

CodeTrail is intended for lawful software development, research, education, and
code-reasoning workflows. This document describes recommended operating
practices. It is **guidance, not an additional restriction on the MIT License**.

## Use only data and systems you are authorized to access

- Process repositories, firmware, documents, images, logs, and model artifacts
  only when you have the necessary authorization.
- Follow applicable laws, contracts, licenses, NDAs, export-control rules,
  platform terms, model-provider terms, and organizational policies.
- Do not use CodeTrail to facilitate unauthorized access, credential theft,
  malware deployment, unlawful surveillance, or other unlawful activity.

## Keep the effective data boundary explicit

CodeTrail is local-first, but the complete workflow includes OpenCode,
llama-server, optional remote endpoints, plugins, project configuration, and the
host operating system. Before handling confidential material:

1. Keep model servers on loopback unless remote access is deliberately required.
2. Keep `enabled_providers` limited to the intended local provider and deny
   OpenCode built-in file, shell, and web tools when the CodeTrail sandbox is the
   required boundary.
3. For untrusted repositories, start with
   `OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode` so project configuration cannot
   silently loosen global permissions.
4. Review all effective model endpoints. A non-loopback endpoint means prompts or
   retrieved content can leave the current machine.
5. Import the minimum necessary files. Do not whitelist an entire home directory
   or a broad shared mount when a narrow source directory will work.

See [docs/security.md](docs/security.md) for the concrete controls and their
limitations.

## Protect inputs and derived artifacts

- Treat `knowledge.json`, embeddings, Code-RAG indexes, imported attachments,
  session databases, logs, telemetry, and backups as potentially sensitive.
- Keep secrets, credentials, signing keys, customer data, and unrelated private
  material outside the project and external-import allowlists.
- Inspect `git status` and `git diff` before committing. The supplied `.gitignore`
  covers CodeTrail's standard generated artifacts, but cannot protect renamed,
  copied, or manually exported data.
- Apply the retention, encryption, access-control, backup, and secure-deletion
  rules required by the data owner.

## Keep a human in the decision loop

LLM output and retrieval results are probabilistic. File citations, high scores,
strict mode, and self-checks reduce some errors but do not establish correctness.

- Review patches and commands before approval; use the smallest necessary scope.
- Run the appropriate project-specific validation before deployment.
- Independently verify security claims, register values, timing limits, hardware
  behavior, and other consequential facts against authoritative sources.
- Do not rely on CodeTrail as the sole decision-maker for safety-critical,
  security-critical, regulated, medical, legal, financial, employment, or other
  high-impact decisions.

## Respect third-party terms

The [MIT License](LICENSE) applies to CodeTrail's own software. Models,
dependencies, source repositories, datasets, documents, and generated artifacts
may be governed by separate terms. Confirm that your use, modification, and
redistribution of each component is permitted, and preserve required notices and
attribution.

For warranty, liability, audit, and confidentiality limitations, read the
[Disclaimer](DISCLAIMER.md).
