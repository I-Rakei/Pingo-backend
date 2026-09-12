---
name: Pingo
description: A calm, exact dark ledger for private debt management across web and mobile.
colors:
  canvas: "#141518"
  sidebar: "#0d0e10"
  surface: "#1c1e22"
  surface-raised: "#25272c"
  surface-interactive: "#292c32"
  surface-hover: "#2d3036"
  border: "#303238"
  input-border: "#35383f"
  text-primary: "#f4f4f5"
  text-strong: "#fafafa"
  text-muted: "#a1a1aa"
  primary: "#ff6c37"
  primary-foreground: "#ffffff"
  success: "#34d399"
  warning: "#fcd34d"
  danger: "#fca5a5"
  destructive: "#ef4444"
  info: "#60a5fa"
typography:
  display:
    fontFamily: "Inter Variable, Inter, sans-serif"
    fontSize: "1.875rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0"
  headline:
    fontFamily: "Inter Variable, Inter, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "0"
  title:
    fontFamily: "Inter Variable, Inter, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.375
    letterSpacing: "0"
  body:
    fontFamily: "Inter Variable, Inter, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
  label:
    fontFamily: "Inter Variable, Inter, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.33
    letterSpacing: "0"
rounded:
  control-sm: "6px"
  control: "8px"
  surface: "8px"
  status: "9999px"
spacing:
  1: "4px"
  2: "8px"
  3: "12px"
  4: "16px"
  5: "20px"
  6: "24px"
  8: "32px"
  9: "36px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0 10px"
    height: "32px"
  button-secondary:
    backgroundColor: "{colors.surface-interactive}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0 10px"
    height: "32px"
  input:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "4px 10px"
    height: "32px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.surface}"
    padding: "16px"
  sidebar-item-active:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.control-sm}"
    padding: "8px"
    height: "36px"
---

# Design System: Pingo

This document is the visual and interaction source of truth for the Pingo web
application and the direction for bringing the Dividas/Pingo mobile client into
closer visual alignment. It describes the system that exists in code as of
2026-09-12, calls out intentional product decisions, and names known mismatches
instead of hiding them.

Implementation sources:

- Web tokens: `../frontend/src/index.css`
- Web primitives: `../frontend/src/components/ui/`
- Web shell: `../frontend/src/components/pingo-sidebar.jsx`
- Mobile tokens: `../../Dividas/constants/theme.ts`
- Product and architecture context: `PRODUCT.md` and `MASTER_CONTEXT.md`

## 1. Overview

**Creative North Star: "The Quiet Ledger"**

Pingo should feel like opening a well-kept ledger in a low-distraction workspace.
The canvas is dark and steady, separators are precise, and color appears only when
it communicates action, progress, or financial state. The interface is not a
marketing surface. It begins with the user's real work and stays out of the way.

The system is compact rather than sparse. Tables, totals, filters, and actions are
close enough for repeated daily use, but hierarchy and spacing prevent the density
from becoming noise. Pingo orange is the recognizable product signal. It is not a
background decoration and must not spread across inactive UI.

The visual system rejects generic blue fintech styling, purple gradients, beige
editorial treatments, glass panels, decorative glow, oversized hero typography,
and repeated eyebrow labels. Familiar product controls are a virtue: a three-dot
menu should look like a menu, a destructive action should look dangerous, and a
table should remain easy to compare row by row.

**Key Characteristics:**

- Dark-only web workspace with layered neutral surfaces
- One strong orange accent used with restraint
- Inter throughout the web application
- Compact controls and fixed, predictable dimensions
- Eight-pixel geometry for cards, fields, menus, and dialogs
- Borders and tonal changes before shadows
- Tables on desktop and purpose-built record lists on mobile widths
- Visible state for loading, empty, disabled, error, success, and focus
- Iconify icons for product actions, usually from the Solar Linear family

**The Work-First Rule.** The first authenticated viewport must show the ledger or
the task the user came to perform. Never place a landing-page hero inside the app.

**The One Accent Rule.** Pingo orange identifies primary action, selection, focus,
and progress. It is not decorative fill.

**The Stable Geometry Rule.** Dynamic labels, loading indicators, badges, icons,
and row actions must not resize their parent controls or shift surrounding layout.

### Layout and density

The web content column is centered and generally capped between 1400px and 1500px.
Page gutters are 16px on compact screens, 24px from the small breakpoint, and 32px
on large screens. Standard vertical page padding is 24px, rising to 32px on large
screens.

The sidebar is a structural navigation rail, not a floating card. It uses the
darkest surface and collapses to icons on desktop. On narrow screens, navigation
uses the existing shadcn sidebar/sheet behavior. The expanded Pingo mark is 48px;
the collapsed mark is 32px.

Page headers use a 24px title on compact screens and 30px at the small breakpoint,
with a short muted description when it adds information. The header ends with a
divider and 24px bottom padding. Do not restore small decorative labels above page
titles.

Repeated record containers are normally full-width bordered sections. Summary
metrics may use individual cards because they are genuinely repeated comparable
items. Page sections themselves must remain unframed.

### Responsive behavior

Responsive adaptation is structural; typography does not scale continuously with
viewport width.

- Under the medium breakpoint, comparison tables become stacked record rows.
- Primary page actions become full-width where necessary.
- Header actions wrap below the title instead of overlapping it.
- Forms move from two columns to one.
- Dialogs use `calc(100% - 2rem)` width and no more than 92svh height.
- Sheets use the full compact width and a restrained maximum width on larger
  screens.
- Long names truncate only in summary rows; profiles and detail views must expose
  the full value.
- Pagination stacks its range label and controls on compact screens.

### Iconography

Use `@iconify/react` for application icons. The current visual language primarily
uses Solar Linear icons, which match Pingo's clean stroke weight. Standard inline
icons are 16px; supporting state icons are commonly 20px; empty-state icons may be
32px. Icons inherit text color.

Use an icon alone only for a universally understood action such as close, edit,
delete, back, download, notification, or a three-dot menu. Every icon-only button
requires an accessible label, and unfamiliar icons require a tooltip. Do not draw
new SVG icons when Iconify already provides the symbol.

### Content and voice

Copy is plain, short, and operational. Name the object and the result:
"Add debt", "Pay interest", "Download receipt", "Edit client". Avoid feature
explanations in the interface, celebratory filler, or marketing claims.

Confirmation text should say what happened. Error text should say what failed and,
where possible, what the user can do next. Financial actions must name whether they
affect interest, principal, one installment, or the complete balance.

## 2. Colors

The Pingo palette is a restrained neutral ladder with one vivid orange product
accent and a small semantic set for ledger state.

The exact normative values live in the frontmatter and in
`../frontend/src/index.css`. Do not create near-duplicate grays in individual
components. Add or change a semantic token centrally.

### Primary

- **Pingo Orange** (`primary`): primary buttons, selected navigation, focus rings,
  active segmented controls, progress, and the most important balance emphasis.
- **Primary Foreground** (`primary-foreground`): the current foreground on orange.
  See the contrast warning below before using it for small text.

Orange should normally occupy less than ten percent of a working screen. A screen
with several large orange surfaces has lost the intended hierarchy.

### Semantic colors

- **Settled Green** (`success`): paid status, successful completion, and incoming
  payment values.
- **Due Amber** (`warning`): unpaid status and approaching due state.
- **Overdue Red** (`danger`): overdue status and error-supporting text.
- **Destructive Red** (`destructive`): delete/revoke actions and invalid controls.
- **Information Blue** (`info`): charts or genuine informational state only. It is
  not a second brand color.

Semantic color never carries meaning alone. Pair it with explicit words such as
Paid, Unpaid, Overdue, Delete, or Error and, where helpful, a distinct icon.

Status badges use a low-opacity semantic background, a subtle same-hue border, and
the brighter semantic foreground. They remain compact, but their text is never
removed in favor of a colored dot.

### Neutral colors

- **Canvas Black** (`canvas`): application content background.
- **Navigation Black** (`sidebar`): sidebar, one step darker than the canvas.
- **Ledger Surface** (`surface`): tables, cards, popovers, dialogs, and sheets.
- **Muted Surface** (`surface-raised`): skeletons and quiet secondary regions.
- **Interactive Surface** (`surface-interactive`): secondary controls.
- **Hover Surface** (`surface-hover`): hover and menu focus states.
- **Ledger Line** (`border`): section separators and container boundaries.
- **Field Line** (`input-border`): the slightly stronger resting input boundary.
- **Primary Text** (`text-primary`): titles, values, and body copy.
- **Strong Text** (`text-strong`): high-emphasis text on dark surfaces.
- **Muted Text** (`text-muted`): descriptions, metadata, placeholders, and labels.

**The Tonal Depth Rule.** Use neutral surface changes and one-pixel separators to
show structure. Do not turn every layer into a shadowed card.

**The Semantic Exclusivity Rule.** Green means settled/success, amber means unpaid
or due, and red means overdue/error/destructive. Never use those colors as random
decoration.

**The No Gradient Rule.** Gradients are prohibited throughout the authenticated
product except the established black fade over the bottom of the login image. Text
gradients are always prohibited.

### Contrast warning

The current web token uses white text on Pingo orange. That combination is not
strong enough for WCAG AA normal-size text. Do not claim the current primary button
is AA-compliant. The design owner should approve one of these reconciliations:

1. Use Canvas Black text on Pingo Orange for small button labels.
2. Darken the button orange while preserving the brighter orange for focus and
   graphical emphasis.
3. Keep white only for sufficiently large/bold text after measured verification.

Until implementation is changed, preserve the current token for consistency but
flag this issue in accessibility reviews. Do not invent a different local orange
inside one component as a workaround.

## 3. Typography

**Display Font:** Inter Variable, with Inter and sans-serif fallbacks

**Body Font:** Inter Variable, with Inter and sans-serif fallbacks

**Label/Mono Font:** Inter Variable. Pingo deliberately uses the same family for
numeric and mono-like web data rather than introducing a code font.

**Character:** Inter keeps the ledger neutral, compact, and highly legible. Weight,
size, position, and tonal contrast create hierarchy; extra font families do not.

### Hierarchy

- **Page title / display** (600, 24px compact and 30px from `sm`, 1.2): one H1 per
  view. Use normal letter spacing and balanced wrapping.
- **Metric** (600, 20px compact and 24px from `sm`, approximately 1.25): summary
  values only. Do not let a metric become a marketing hero.
- **Section title** (600, 16px, 1.375): table headings, dialog titles, and major
  grouped content.
- **Body** (400, 14px, 1.5): descriptions, field content, and normal UI copy.
- **Strong body** (500 or 600, 14px): names, important values, and action labels.
- **Label** (500, 12px, 1.33): field labels, metadata, pagination summaries, and
  badges.
- **Micro label** (500, 10px, uppercase only when the data category benefits):
  compact metric labels inside a profile summary. This is not a page eyebrow.

Use tabular numerals for aligned financial columns and counters where the browser
style supports it. Keep MZN on the right as a suffix:

```text
59,800.00 MZN
```

Do not abbreviate to `59.8k` in debt balances, payments, invoices, receipts, or
audit records. Precision is part of the product's trust.

Body prose should remain under roughly 70 characters per line. Tables can be much
wider because comparison, not reading flow, is their purpose.

**The One Family Rule.** Inter is the complete web type system. IBM Plex, Lato,
Manrope, serif display fonts, and decorative monospace are prohibited in new web
UI.

**The Zero Tracking Rule.** Letter spacing is zero. Do not use negative tracking,
and do not add wide tracking to uppercase labels.

**The Data First Rule.** Financial values are never lighter, smaller, or less
legible than the decorative label describing them.

### Mobile typography divergence

The mobile repository currently contains Lato tokens and some hard-coded Manrope
drawer styling. This predates the explicit Inter-only web direction. Treat it as
legacy divergence, not a pattern to copy back into the web app.

A mobile typography migration must be a deliberate, tested project: load Inter in
Expo, replace token references consistently, test Android font metrics and line
wrapping, and verify invoices/PDFs separately. Do not partially mix Inter into a
few mobile screens.

## 4. Elevation

Pingo is flat by default. Hierarchy comes from the four neutral surface levels,
one-pixel borders, dividers, and modal backdrops. Shadows are reserved for content
that genuinely floats above the document: menus, dialogs, sheets, and tooltips.

Normal summary cards and table containers use no drop shadow. Existing page cards
often override generated Card shadow behavior with `shadow-none` and use a border
or subtle ring. This is the intended application pattern.

### Shadow vocabulary

- **No elevation:** inline sections, summary cards, table containers, filter bars,
  profile summaries, and static form groups.
- **Menu elevation:** a compact medium shadow plus a subtle foreground ring. Use
  only for portal-rendered dropdown content.
- **Sheet elevation:** a directional large shadow while the sheet is open.
- **Dialog elevation:** a strong shadow against a 70% black backdrop with a very
  light blur. The backdrop supplies separation more than ornamental shadow.
- **Tooltip elevation:** none beyond its inverted foreground surface; its contrast
  creates the layer.

Do not pair a one-pixel border with a wide soft shadow on ordinary cards. Pick the
border. Floating surfaces may use both a subtle edge ring and a purposeful compact
shadow because they must detach from underlying content.

**The Flat-at-Rest Rule.** Static content rests on the canvas. Elevation appears
only when a component is spatially above another component.

**The Portal Rule.** Menus, dialogs, sheets, and tooltips must render through their
provided Base UI portals. Do not place an absolutely positioned menu inside an
overflow-hidden table container.

## 5. Components

Use the existing shadcn/Base UI primitives before adding a new abstraction. The
primitive provides accessibility mechanics; the composed feature component owns
domain copy and behavior. Iconify supplies product icons.

Every interactive component must account for default, hover, focus-visible,
active, disabled, loading, and invalid/error states where those states apply.

### Buttons

**Character:** compact, direct, and stable.

- **Shape:** gently squared corners (8px).
- **Default size:** 32px high, 14px medium text, 10px horizontal padding, 6px gap.
- **Large size:** 36px high; use only where a form or page action needs more touch
  room.
- **Icon size:** 32px square by default; 24px, 28px, and 36px variants exist.
- **Primary:** orange fill and current primary foreground; reserved for the single
  strongest action in a local region.
- **Outline:** canvas/transparent fill with a border; used for secondary commands.
- **Secondary:** interactive neutral surface; use when outline would be too faint.
- **Ghost:** no resting container; used for table tools and low-priority commands.
- **Destructive:** translucent red surface and red label; delete/revoke only.
- **Link:** orange text with underline on hover; navigation inside prose only.
- **Focus:** orange border plus a three-pixel 50%-opacity ring.
- **Active:** one-pixel downward translation on direct action buttons.
- **Disabled:** no pointer events and 50% opacity.
- **Loading:** replace the leading icon with the shared 16px spinner, retain the
  button width, and keep a meaningful loading label when space permits.

Do not place multiple primary orange buttons side by side. Payment flows may use
green for a clearly labeled "Pay in full" success action, but orange remains the
default action color.

### Inputs and text areas

**Character:** quiet until focused, explicit when invalid.

- **Shape:** 8px radius.
- **Height:** 32px for standard single-line fields; composed selectors may use
  36px where they include an avatar or secondary value.
- **Fill:** transparent over canvas with a subtle input tint in dark mode.
- **Stroke:** one-pixel field boundary.
- **Padding:** 10px horizontal; 4px vertical for inputs and 8px for text areas.
- **Typography:** 16px on compact screens to prevent mobile browser zoom, 14px from
  the medium breakpoint.
- **Placeholder:** muted text, still required to meet contrast expectations.
- **Focus:** orange border and three-pixel translucent orange ring.
- **Invalid:** destructive border and translucent destructive ring, accompanied by
  readable error text with `role="alert"`.
- **Disabled:** stronger muted fill, disabled cursor, 50% opacity.

All web fields set `autoComplete="off"` and password-manager ignore hints because
the user explicitly requested no browser prefilling. New fields must preserve
this behavior unless authentication security requires a deliberate exception.

Labels stay outside fields. Prefix icons are 16px and muted until the field is
focused or invalid. Numeric fields must not show browser-generated historical
value menus.

### Client selector

Client name in Add Debt is a dropdown selector, not a free-form browser-autofilled
field. Its trigger uses the same field boundary and focus ring as inputs, can show
client metadata on wider screens, and opens a portal-rendered menu. The selected
client is visually marked; client creation remains a separate explicit workflow.

Base UI group labels must be descendants of `Menu.Group` or `Menu.RadioGroup`.
Violating that composition triggers `MenuGroupContext is missing` at runtime.

### Cards and containers

**Character:** bounded data regions, never decoration.

- **Corner style:** 8px for application surfaces. The generated Card primitive has
  a 12px default, but composed Pingo screens normalize it with `rounded-lg`.
- **Background:** ledger surface.
- **Boundary:** one-pixel border or subtle ring, not both plus a large shadow.
- **Padding:** 16px standard, 12px compact, 20px for complex modal sections.
- **Spacing:** 12px compact and 16px standard internal rhythm.

Use a card for a repeated summary metric, a modal-contained preview, or a genuinely
bounded tool. Do not place cards inside cards. Do not wrap every page section in a
floating panel.

### Summary metrics

Dashboard and Debts use the same compact metric-card vocabulary. Each metric has a
small muted label, a strong amount or count, and optional semantic tone. Cards stay
equal height and use stable grid tracks. They do not include decorative icons by
default.

Profile summary strips use a single bordered grid with one-pixel gaps and surface
cells. This preserves comparison without creating five separate floating cards.

### Tables

**Character:** scan-first, consistent, and action-ready.

- Table containers use an 8px radius, ledger surface, and one-pixel border.
- Headers use small, strong text with clear column alignment.
- Names and primary identifiers align left; amounts align right.
- MZN follows the number.
- Metadata uses muted text but remains readable.
- Rows use one-pixel separators and a restrained hover surface.
- The final column reserves stable width for the three-dot action menu.
- Action menus include icons and labels. They never use a bare navigation arrow.
- Paid rows may reduce emphasis, but never enough to become unreadable.
- Empty state lives inside the table container and names the next available action.

All frontend tables and ledger lists paginate at ten records per page. Pagination
shows the visible range and total count, Previous/Next controls, page numbers, and
ellipses for long ranges.

At widths below `md`, replace the table with structured list rows. Do not force a
desktop table into horizontal overflow when a record card/list can preserve the
same hierarchy. Mobile list rows must expose the same critical values and actions.

### Status badges

Badges are 20px high with full-pill geometry, 12px medium text, subtle border, and
horizontal padding. Full-pill radius is permitted because status is a compact tag,
not a container.

- Unpaid: amber label, amber-tinted surface, amber border
- Overdue: red label, red-tinted surface, red border
- Paid: green label, green-tinted surface, green border

Use the canonical words consistently. Do not switch between Settled and Paid in
record-level state unless the surrounding aggregate specifically uses Settled as
its metric label.

### Segmented controls and tabs

Use segmented controls for mutually exclusive compact modes such as debt filters
or Periods/Payments. The container is 36px high with canvas background and a
one-pixel boundary. Options are 28px high, 12px medium/semibold, and use orange fill
only when selected. Unselected options are muted and gain foreground contrast on
hover.

Tabs change the visible data set without implying navigation to a new entity.
Reset pagination to page one when the tab or filter changes.

### Dropdown menus

Menus use an 8px radius, ledger surface, 4px padding, subtle ring, and compact
shadow. Items are at least 36px high in domain action menus even if the primitive
supports tighter defaults. Menu items use a 16px icon plus a clear verb label.

Order actions by frequency and risk. Put destructive actions last and give them
destructive color. The receipt option appears for paid records; invoice appears
for unpaid/overdue records. Debt menus can expose debt profile, relevant payment
actions, document download, edit, and delete.

### Dialogs and sheets

Use a dialog for a focused, bounded action that needs confirmation or review: Add
Debt, record payment, settle, or a destructive confirmation. Add Debt is a modal
and must not become a sidebar destination.

Use a right sheet for editing a record when the user benefits from retaining the
underlying list/profile context. Client and debt editing follow this pattern.

Dialogs use an 8px radius, 70% black backdrop, maximum 92svh height, bordered
header/footer, and internal scroll where needed. Sheets are full width on compact
screens and capped around 448px on larger screens. Close buttons use an icon and an
accessible name.

Footer actions stack in reverse order on compact screens so the primary action is
easy to reach, then align right in a row at the small breakpoint.

### Navigation

The sidebar uses Navigation Black, a one-pixel boundary, and five destinations:
Dashboard, Debts, Clients, History, and Settings. Active items use Pingo Orange;
hover uses the sidebar neutral accent. Items are 36px high with 8px padding and a
16px Iconify icon.

The logo area is 64px high and includes the 48px product mark, Pingo name, and
"Debt tracker" description while expanded. In icon mode, text disappears and the
mark becomes 32px. Tooltips identify collapsed destinations.

The sidebar must not contain Add Debt. It is a command, not a destination.

### Pagination

The shared `DataPagination` owns range calculation and controls. It shows
`Showing X-Y of Z` in 12px muted text. Current page uses an active bordered control;
disabled Previous/Next remain visible so the layout does not jump.

Pagination controls must not change width as pages change. Long result sets use
ellipses; do not render dozens of page buttons.

### Loading, empty, and error states

Use skeletons for page and content loading. A skeleton mirrors the approximate
geometry of the interface it replaces, uses the muted surface, and pulses unless
the user prefers reduced motion.

Use the shared 16px spinner inside an action that is already in progress. Do not
replace an entire content area with centered "Loading..." text.

Empty states belong where the content would appear. Use one restrained 32px muted
icon, a direct title, a one-line explanation, and the next valid action when one
exists. Do not turn empty states into illustrations or marketing panels.

Errors appear near the failed action in readable danger text and use `role="alert"`.
Keep user input intact after validation or network failure. Global auth expiration
returns to login; it must not leave stale account data visible.

### Notifications

The web notification center is a utility surface derived from current debt state.
The notification icon uses standard toolbar sizing; unread/current state needs a
textual accessible name. Browser permission is requested only in direct response
to a user action.

Push notification content is concise and identifies the debt/reference or due
state. A click may deep-link with `?debt=PNG-...`, which the application resolves
to the debt profile.

### Login and registration

On large screens, the Pingo image occupies the left side. A black gradient is
allowed only at the bottom of that image to protect overlaid content. The form is
on the right, uses the same dark tokens and controls as the app, and remains usable
without the image on compact screens.

The Pingo logo is a first-viewport signal. Inputs must stay above the virtual
keyboard and must not allow browser autofill to obscure intentional values.

### Motion

Motion communicates state and spatial relationship; it never decorates the ledger.

- Input, button, and color feedback: approximately 150ms.
- Dropdown open/close: 100ms fade/scale or directional slide.
- Dialog open/close: 150ms fade and 95%-to-100% scale.
- Sheet open/close: 200ms directional translation and fade.
- Sidebar width transition: 200ms.
- Progress bars may transition width after data changes.
- Button press may translate down by one pixel.

Use ease-out behavior for entering and standard/ease-in for leaving. No bounce,
elastic movement, page-load choreography, or animated financial numbers.

Every animation must have a reduced-motion alternative. Existing skeleton and
spinner primitives stop their animation under `prefers-reduced-motion`. New motion
must do the same and must leave content visible without waiting for animation.

### Accessibility and interaction

- Target WCAG 2.2 AA.
- Never remove the visible focus ring.
- Every icon-only control has an `aria-label` and, when unfamiliar, a tooltip.
- Use real buttons for actions and links only for navigation.
- Dialogs and menus must retain Base UI focus management and keyboard behavior.
- Touch targets on mobile should reach at least 44px even when the visible control
  is smaller; use hit slop or the primitive's expanded hit area.
- Error and success are announced appropriately and never encoded only by color.
- Table headers describe their columns; mobile list alternatives retain labels.
- Text must fit without overlapping controls at narrow and wide viewports.
- Truncation must have a detail view or another way to inspect the full value.
- Currency and dates must be formatted consistently and read in logical order.

### Mobile alignment

The mobile app currently defines both light and dark palettes, uses Lato tokens,
and contains Manrope in drawer configuration. The web product is explicitly
dark-only and Inter-only. Until a dedicated mobile visual migration is completed:

- Preserve mobile behavior and installed-data compatibility.
- Reuse Pingo Orange, 8px functional radii, semantic status meanings, and compact
  spacing across both clients.
- Do not copy mobile light surfaces into web.
- Do not copy web-only Base UI assumptions into React Native.
- Treat mobile component implementation as platform-native while preserving the
  same hierarchy, wording, and state meaning.
- Test keyboard, safe areas, touch sizes, and offline states on a release APK.

## 6. Do's and Don'ts

### Do:

- **Do** use Canvas Black, Navigation Black, and Ledger Surface to create quiet
  depth before adding a shadow.
- **Do** reserve Pingo Orange for primary actions, active selection, focus, and
  progress.
- **Do** use Inter Variable for every new web heading, label, value, input, button,
  and data display.
- **Do** use normal letter spacing (`0`) throughout the web application.
- **Do** keep functional cards, fields, menus, dialogs, and sheets at an 8px corner
  radius unless the existing primitive requires a smaller control radius.
- **Do** use full-pill geometry only for compact statuses, switches, and true tags.
- **Do** place MZN after the amount and preserve two-decimal financial precision.
- **Do** keep tables at ten records per page and provide a structured mobile list
  presentation below the medium breakpoint.
- **Do** use a three-dot Iconify menu with labeled actions for table row commands.
- **Do** show receipt only for paid records and invoice for unpaid/overdue records.
- **Do** give debts their own profile rather than routing debt actions through the
  client profile.
- **Do** use skeletons for content loading and the shared spinner for in-progress
  buttons.
- **Do** preserve form values and local mobile data when a request fails.
- **Do** use borders and gap rhythm to group dense operational data.
- **Do** keep focus, disabled, invalid, loading, success, and empty states complete.
- **Do** make every icon-only command keyboard accessible and explicitly labeled.
- **Do** respect reduced motion and keep all content visible without animation.
- **Do** verify wide desktop and narrow mobile layouts before shipping.

### Don't:

- **Don't** use IBM Plex or any IBM font in the web application.
- **Don't** introduce a web light theme or an Appearance/theme switch.
- **Don't** turn an authenticated screen into a SaaS marketing page.
- **Don't** use blue-and-purple generic fintech dashboards.
- **Don't** use beige editorial layouts or paper metaphors.
- **Don't** use glassmorphism, gradient text, decorative orbs, glow blobs, bokeh,
  or repeating stripe backgrounds.
- **Don't** use gradients anywhere except the established black bottom fade on the
  login image.
- **Don't** add tiny uppercase eyebrow labels such as "Portfolio", "12 clients",
  "Ledger archive", or "Workspace" above page titles.
- **Don't** make page sections into floating cards or put cards inside cards.
- **Don't** exceed 8px radius on application cards and framed tools. Generated
  primitive defaults must be normalized in composed screens.
- **Don't** pair a bordered static card with a wide soft shadow.
- **Don't** use oversized hero typography inside dashboards, tables, dialogs,
  settings, or profiles.
- **Don't** scale font size continuously with viewport width.
- **Don't** use negative letter spacing.
- **Don't** use color alone for paid, unpaid, overdue, or destructive meaning.
- **Don't** abbreviate ledger amounts or move MZN to the left of the value.
- **Don't** use navigation arrows as table actions. Use the three-dot menu.
- **Don't** put Add Debt in the sidebar. It is a modal command.
- **Don't** expose Client Profile when the requested destination is Debt Profile.
- **Don't** show raw "Loading..." text where a skeleton or button spinner belongs.
- **Don't** allow browser autocomplete to prefill operational forms.
- **Don't** place Base UI menu group parts outside their required group context.
- **Don't** render dropdowns inside an overflow-clipped table container; use the
  primitive portal.
- **Don't** invent hand-drawn SVG icons when Iconify provides the action.
- **Don't** add bounce, elastic motion, or orchestrated page-load sequences.
- **Don't** partially restyle mobile typography without testing the full app,
  Android font metrics, PDFs, keyboard behavior, and existing installed data.

If a new screen follows these rules, it should feel native to Pingo before the logo
is visible: dark, measured, precise, and unmistakably focused on the ledger.
