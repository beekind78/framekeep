# Security policy

## Reporting a vulnerability

Use **Report a vulnerability** on the [GitHub Security Advisories page](https://github.com/beekind78/framekeep/security/advisories). This sends the report to the maintainer privately. If the button is unavailable, open an issue asking for a private contact and leave the technical details out of the issue.

Include the Framekeep version or commit, your Windows version, and the shortest steps that show the problem. Describe what an attacker would need and what they could read, change, or cause. If possible, reproduce the issue with a generated or synthetic file. Keep personal media, file paths, library databases, credentials, and full logs out of reports.

Framekeep is maintained on `main`. If you can, check whether the problem also occurs on the latest version. A report about an older version is still useful when it identifies the version and impact.

## What to report

Framekeep is a single-user Windows photo library. Its desktop window talks to a local service. The app reads media and metadata from folders a user selects, and stores catalog information, previews, and face data on that PC. Files in a backup can come from untrusted sources.

Please report issues that break these boundaries:

- Reading library data or triggering actions without an authorized local session.
- Reading or writing beyond the selected source or approved output folder.
- Modifying original media during scanning, editing, recovery, or export.
- Using a crafted image, video, or metadata file to run code or bypass processing limits.
- Exposing private media, catalog information, or credentials through the desktop window, logs, or publication process.

Local catalog files are stored without encryption. Media workers run with the user's permissions and have processing limits. See the [technical reference](docs/TECHNICAL.md#local-data) for the storage model and the [worker limits](docs/TECHNICAL.md#original-file-processing).

Use [GitHub Issues](https://github.com/beekind78/framekeep/issues) for ordinary bugs and feature requests.
