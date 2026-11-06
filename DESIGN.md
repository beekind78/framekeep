# Framekeep interface

Framekeep's gallery puts thumbnails, dates and filenames together so a collection can be browsed without opening each file. The layout supports filtering a month, comparing pictures and acting on a selection.

## Direction

A dark forest navigation rail sits beside a light gallery. It separates source folders and library views from the pictures. Green marks selection and primary actions; warm red marks removal. Page headings show the current view and item count, with instructions beside the controls that need them.

The redesign used [Antislop](https://antislop.ai.id/) and [Style UI](https://style.ai.id/) as references for hierarchy, contrast and restrained decoration. The application does not depend on either site or bundle their interface code.

The startup screen reuses Framekeep's logo. The four corners expand, rotate twice as a frame and return to their original position. The picture stays still. On fast starts the app shows one short cycle before opening the library. Reduce Motion removes that wait and shows the static logo. The loading and card feedback draw on [Bencho's loading state](https://bencho.dev/blocks/loading-state) and [album shelf](https://bencho.dev/blocks/album-shelf).

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
- A thin line below the header marks an in-progress gallery request. Active filters appear below the search controls and can be removed individually.
- Photo cards hold their size while a preview loads. The preview fades in when decoded, and an unavailable preview leaves the photo accessible from the card.
- Album date filters are grouped in an accessible disclosure and expand when a date is active. Opening an album returns to the page heading.
- Selected cards have an explicit checkbox and outline. A bottom toolbar provides Copy, Organize, Remove and Clear actions. It uses the existing verified copy and recoverable removal paths.
- Ctrl/Shift clicks and Ctrl+A, Ctrl+C and Delete retain their existing behavior. Removal always requires confirmation and preserves original files.
- The viewer uses a dark image surface and a readable details column. Video playback uses the existing local player.
- Small windows reflow controls and reduce grid columns; touch controls retain usable target sizes. Animations respect reduced-motion preferences.

## Figma and privacy

The [Figma companion](https://www.figma.com/design/a1Hj30Ogrd9oVQlxw3nW39?node-id=10-5) uses generated sample artwork and demo filenames. It contains no personal library data. Capture uses a separate static demo export, so the installed app keeps its content security policy. The [publishing notes](docs/PUBLISHING.md) describe how source releases exclude personal media, catalogs and runtime credentials.
