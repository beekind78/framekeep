# Contributing to Framekeep

Bug reports, documentation corrections, and small fixes are welcome. For a larger change, open an issue first to discuss what would change for users.

## Report a bug

Use the [bug report template](https://github.com/beekind78/framekeep/issues/new?template=bug_report.md). Include your Framekeep and Windows versions, the steps to reproduce the problem, and what you expected to happen. Use the generated sample library or a synthetic test file when you can. Remove account names and local paths from excerpts and screenshots before posting.

Use the [security policy](SECURITY.md) for vulnerabilities and the [code of conduct](CODE_OF_CONDUCT.md) for participation guidelines.

## Make a change

1. Fork the repository and create a branch for your change.
2. Follow the [development setup](docs/TECHNICAL.md#development). Test with the sample library or files you created for the change.
3. Run the [project checks](docs/TECHNICAL.md#tests). If you change the desktop interface, also try the affected workflow in the Windows app.
4. Open a pull request. Explain what changed, how you tested it, and any conditions needed to reproduce the result.

Keep source photos, catalog data, export manifests, model weights, and local diagnostics outside the commit. The [publication guide](docs/PUBLISHING.md) explains how to check staged files before sharing them.
