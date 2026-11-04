# Framekeep interface

Framekeep is a desktop photo library for browsing large phone backups. The pictures should take precedence over interface decoration. Most sessions involve filtering a month, comparing thumbnails, selecting a group and copying or removing it from the index.

## Direction

Use a dark forest navigation rail alongside a light, neutral gallery. Keep the existing Framekeep mark. The rail separates navigation from content without enclosing every control in a card. Green identifies selection and primary actions; warm red is reserved for removal. Display factual counts and short instructions instead of promotional headings.

The redesign takes inspiration from [Antislop](https://antislop.ai.id/) and [Style UI](https://style.ai.id/): purposeful visual hierarchy, readable contrast, restrained decoration and controls chosen for the task. These are references, not dependencies or bundled third-party interface code.

## Foundations

| Role | Value |
| --- | --- |
| Canvas | `#f7f8f5` |
| Surface | `#ffffff` |
| Navigation | `#183e33` |
| Primary text | `#20342c` |
| Secondary text | `#63736a` |
| Action | `#286149` |
| Border | `#dde4de` |
| Removal | `#a23e35` |
| Typeface | DM Sans, bundled locally under SIL OFL 1.1 |
| Type scale | 34 px page heading, 21 px album title, 17 px month heading, 12–14 px controls and captions |
| Spacing | 4, 8, 12, 16, 24, 32 px, with optical adjustments for thumbnails |

The readable stylesheet is `framekeep/static/workspace.css`. Existing specialized controls retain their base styles in `style.css`. All application fonts and interface assets are served locally. No external UI library, font service, analytics or capture script runs in the installed application.

## Gallery behavior

- Comfortable and compact grids share the same files, filters and selection. The preference is stored in browser-local storage; changing density does not refresh images or clear selection. The desktop's private WebView profile starts a fresh preference when the window is relaunched.
- Album date filters are grouped in an accessible disclosure and expand when a date is active. Opening an album returns to the page heading.
- Selected cards have an explicit checkbox and outline. A bottom toolbar provides Copy, Organize, Remove and Clear actions. It uses the existing verified copy and recoverable removal paths.
- Ctrl/Shift clicks and Ctrl+A, Ctrl+C and Delete retain their existing behavior. Removal always requires confirmation and preserves original files.
- The viewer uses a dark image surface and a readable details column. Video playback uses the existing local player.
- Small windows reflow controls and reduce grid columns; touch controls retain usable target sizes. Animations respect reduced-motion preferences.

## Figma and privacy

The [editable companion design](https://www.figma.com/design/a1Hj30Ogrd9oVQlxw3nW39?node-id=10-5) uses generated sample artwork and demo filenames. Personal libraries, original photos, catalog data and runtime capabilities must never be uploaded. A separate static demo export is used for capture; the installed app retains its strict content security policy.
