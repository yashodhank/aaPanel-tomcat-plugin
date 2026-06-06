---
name: javahost-ui
description: >
  Editing JavaHost's single-file index.html UI — enforce WCAG 2.2 AA + WAI-ARIA
  APG for a vanilla, CSP-safe, offline aaPanel plugin modal; no frameworks, no
  CDNs/fonts, inline CSS/JS only. Use when touching plugin/javahost/index.html:
  adding/changing dialogs, popover menus, the section nav/tabs, forms, buttons,
  toasts/alerts/status messages, empty states, icons, colors/contrast, theming,
  focus behavior, keyboard handling, or responsive layout. Distilled from the W3C
  WCAG 2.2 quick-ref and the WAI-ARIA Authoring Practices Guide (cited inline);
  rules apply to the `.jh`-scoped markup so changes stay accessible and don't leak
  styles into the host panel. Pair with the javahost-dev skill for repo/build rules.
---

# JavaHost — UI accessibility guide (index.html only)

Scope: this skill governs **`plugin/javahost/index.html`** only — JavaHost's single,
self-contained admin UI. Its job is to keep every UI edit **WCAG 2.2 AA** and
**WAI-ARIA APG** compliant *under the project's hard constraints*. For repo layout,
endpoints, build/test/deploy, and the clean-room rule, defer to the **javahost-dev**
skill. This file is **instructions only** — no scripts, nothing fetched at runtime.

Authoritative sources (read these, don't guess):
- WCAG 2.2 AA quick-ref: https://www.w3.org/WAI/WCAG22/quickref/?levels=aa
- ARIA APG patterns index: https://www.w3.org/WAI/ARIA/apg/patterns/
- Dialog (Modal): https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/
- Menu / Menu Button: https://www.w3.org/WAI/ARIA/apg/patterns/menu/
  and https://www.w3.org/WAI/ARIA/apg/patterns/menu-button/
- Disclosure: https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/
- Alert: https://www.w3.org/WAI/ARIA/apg/patterns/alert/
- Tabs: https://www.w3.org/WAI/ARIA/apg/patterns/tabs/

---

## 1. HARD CONSTRAINTS (break these and the plugin breaks)

These come from how aaPanel loads the file and from the project's security posture.
Verify against the file's top comment block and the `.jh{ ... }` token block.

1. **One self-contained file.** Everything ships in `index.html`: one inline
   `<style>`, one inline `<script>`, inline-SVG `<symbol>` sprite. No separate
   `.css`/`.js`, no imports.
2. **Inline only, no network.** WHY: the panel runs offline and behind a strict
   CSP; any external request can be blocked or leak. **NO** CDN links, web fonts
   (`@font-face`/Google Fonts), `<link rel>`, remote images, analytics, or
   `fetch()` to third parties. Icons are inline SVG; fonts are the **system stack**
   already in `--font`/`--mono`. Data comes only from the panel AJAX convention
   (`POST /plugin?...&s=<Method>`, `{status,msg}` envelope) via the existing
   `call()` helper.
3. **Vanilla JS, no build.** ES5-compatible style as in the file (`var`, function
   declarations, `Array.prototype.slice`). No bundler, no TypeScript, no JSX, no
   npm deps. jQuery is used **only if the host already provides `$`**; always keep
   the Fetch fallback.
4. **Scoped CSS — never leak into the host modal.** WHY: aaPanel injects this
   markup *inside its own modal DOM*; a bare `button{}` or `table{}` rule would
   restyle the panel. **Every selector must start with `.jh`** (e.g. `.jh-btn`,
   `.jh table`, `.jh input`). Do **not** add bare element selectors, `*` resets
   outside `.jh`, `body`/`html`/`:root` rules, or global `@keyframes`/CSS vars
   defined outside `.jh`. New CSS custom properties go **on `.jh`**.
5. **Cap the root to the viewport, scroll internally.** Keep `#jh-root.jh` with
   `max-height:100vh; overflow-y:auto; overflow-x:hidden`. WHY: the host modal is
   fixed-size; content must scroll *inside* JavaHost, never push off-screen. Don't
   add `position:fixed` full-bleed elements except the existing overlay/toasts.
6. **Class-prefix new components** with `jh-` and register icons as `<symbol
   id="i-...">` in the existing sprite; reference via `<use href="#i-...">`.

---

## 2. ACCESSIBILITY — WCAG 2.2 AA + APG patterns

Each rule states the **WHY** and the **success criterion / APG pattern**.

### 2.1 Modal dialog (the shared `#jh-overlay` / `#jh-modal`)
APG Dialog (Modal). The container already has `role="dialog" aria-modal="true"
aria-labelledby="jh-modal-title"`. When you touch `openModal`/`closeModal`:
- **Label & describe.** Keep `aria-labelledby` → the visible title; if there's a
  subtitle/intro, add `aria-describedby` → that node (SC 4.1.2 / 1.3.1).
- **Move focus IN on open.** On open, focus the first interactive element (or the
  dialog itself if none). WHY: a keyboard/SR user must land inside, not be stranded
  behind it.
- **Trap Tab inside while open.** Tab from the last focusable wraps to the first;
  Shift+Tab from the first wraps to the last. Nothing in the host page may receive
  focus while the dialog is open (SC 2.4.3, APG keyboard interaction).
- **Esc closes** the dialog (and only the topmost layer). WHY: APG-required escape.
- **RETURN focus on close.** Save the element that opened the dialog and
  `.focus()` it back after close (SC 2.4.3). WHY: don't dump focus to `<body>`.
- **Inert the background.** Visually the overlay covers it; ensure background
  controls aren't tabbable (focus trap satisfies this without `inert`).
- The body scroll region (`.jh-modal-b`, `max-height:70vh; overflow:auto`) is fine;
  keep it so content never escapes the modal.

### 2.2 Popover menus & disclosures (`.jh-menu` / `.jh-menu-pop`)
Pick the **right pattern**; don't half-implement `role="menu"`.
- **Row action popovers** are an actions menu → APG **Menu Button + Menu**. The
  trigger keeps `aria-haspopup="true"` and toggles `aria-expanded`. WHY (and a
  known gap to fix when editing): if you use `role="menu"`, the container needs
  `role="menu"` and each item `role="menuitem"`, with **Arrow Up/Down** to move
  between items, **Home/End**, **Esc** to close, and focus returning to the trigger
  on close. If you don't implement arrow-key item navigation, **don't** use
  `role="menu"`/`menuitem` — instead make it a **Disclosure** (button toggling
  `aria-expanded` over a plain group of `.jh-btn`s) so AT expectations match
  behavior. Either way: **Esc closes and returns focus to the trigger**; clicking
  outside closes it.
- Only **one** popover open at a time (close others first), matching current JS.

### 2.3 Section navigation vs. APG Tabs (choose one; don't mix)
WHY: the current `.jh-nav` mixes signals — it has `role="tablist"`/`role="tab"`
**and** `aria-current="page"`. That's contradictory: tabs use `aria-selected`,
site nav uses `aria-current`. When you edit this area, converge on **one** model:
- **Treat it as in-page Tabs (APG Tabs)** — recommended, since panels are
  show/hide regions: container `role="tablist"`; each control `role="tab"` with
  `aria-selected="true|false"` (not `aria-current`) and `aria-controls` → its
  panel; **roving tabindex** (active tab `tabindex="0"`, others `-1`); Left/Right
  (and Up/Down for vertical) move and activate, Home/End jump to ends — this arrow
  logic already exists in `showSection`. Each panel is `role="tabpanel"
  aria-labelledby="<tab id>" tabindex="0"`.
- **OR treat it as site nav** — `<nav aria-label="Sections">`, plain links/buttons,
  active item marked `aria-current="page"`, **remove** all `role="tab*"`.
- Do **not** ship both `aria-selected` and `aria-current` on the same control.

### 2.4 Status messages (SC 4.1.3) — announce without stealing focus
- **Success / info / progress** → a container with `role="status"` (implicit
  `aria-live="polite"`). The toast region `#jh-toasts` already has
  `aria-live="polite"`; keep it and **never move focus** to a toast.
- **Errors / failures** → `role="alert"` (implicit `aria-live="assertive"`) so AT
  interrupts. Inline error blocks (`.jh-alert.danger`) that appear after an action
  must carry `role="alert"`.
- WHY: SC 4.1.3 requires programmatic announcement of state changes *without*
  focus change. The live-region node must exist in the DOM **before** you inject
  text (don't create the region and fill it in the same tick).

### 2.5 Keyboard operability
- **Everything operable by keyboard** (SC 2.1.1): all actions reachable and
  triggerable via Tab/Enter/Space/arrows. No mouse-only handlers (no bare
  `mousedown`/hover-only menus).
- **No keyboard trap** (SC 2.1.2): the only intentional trap is the open modal,
  and it releases on Esc/close. Nothing else may trap Tab.

### 2.6 Focus visibility & integrity
- **Visible focus** (SC 2.4.7): keep `:focus-visible` outlines on every
  interactive class (`.jh-btn`, `.jh-tab`, inputs, `.jh-x`, menu items). Never set
  `outline:none` without an equally visible replacement (the input focus-ring via
  `box-shadow` is an acceptable replacement).
- **Focus not obscured** (SC 2.4.11, AA): a focused element must not be fully
  hidden by sticky bars, the overlay, or popovers. Mind `.jh-side` (sticky) and
  z-index stacking when adding fixed elements.
- **Focus appearance** (SC 2.4.13): the focus indicator must be large/contrasty
  enough — keep `outline:2px` + `outline-offset:2px` (or the 3px ring); don't
  shrink it to a hairline.

### 2.7 Target size (SC 2.5.8, AA)
Interactive targets are **≥24×24 CSS px** (or have ≥24px spacing). WHY: small
icon buttons fail. `.jh-btn.icon` (32×32) and `.jh-x` (32×32) pass; if you add a
smaller hit area, pad it up to 24px min or space neighbors apart.

### 2.8 Name, Role, Value (SC 4.1.2)
- **Icon-only buttons need an accessible name**: `aria-label` (e.g. the Refresh
  button, `.jh-x`, the `data-menu` dots button). A `title` alone is not reliable.
- **Every form control has a programmatic label**: `<label for="id">` ↔ input
  `id` (the create/JAR/WAR/DB forms already do this — keep the pairing on any new
  field). Placeholder is **not** a label.
- **Decorative SVG is hidden**: icons carry `aria-hidden="true"` (and
  `focusable="false"` where relevant), as the sprite and `ic()` helper do. Don't
  expose decorative glyphs to AT.

### 2.9 Color is not the only signal (SC 1.4.1)
Status must not rely on hue alone. Keep the **text label + dot/icon** combo:
badges say "installed/not installed/up/locked", alerts pair an icon with text.
Don't reduce a state to "green vs red" with no words/shape.

### 2.10 Reduced motion (SC 2.3.3, AAA — adopt anyway)
The file has fade/pop/slide/spin keyframes. Wrap non-essential animation in
`@media (prefers-reduced-motion: reduce){ .jh ... { animation:none; transition:none } }`.
WHY: respect users who get motion sickness; spinners may stay but page/modal
transitions should not.

---

## 3. DESIGN TOKENS · CONTRAST · THEMING

Colors live as CSS custom properties on `.jh` with **semantic names**
(`--ink`, `--accent`, `--hi`, `--ok`, `--danger`, `--text`, `--muted`, `--surface`,
`--line`, ...). Always reference tokens, never hardcode hex in component rules.

**Contrast floors (must pass):**
- **SC 1.4.3 (text):** body/normal text **≥ 4.5:1** against its background;
  **large text** (≥24px, or ≥18.66px bold) **≥ 3:1**.
- **SC 1.4.11 (non-text):** UI component boundaries, icons that convey meaning,
  focus rings, and graphical state indicators **≥ 3:1** against adjacent colors.
- **EXPLICIT WARNING:** `--accent #7fd1e0` and `--hi #f0a830` are **light** and
  **fail 4.5:1 as body text** on white/light surfaces. Use them only for **fills,
  large headings, badges-with-dark-text, borders, and focus accents**. For text on
  light surfaces use the darkened pair **`--accent-d #3a8ca0`** / **`--hi-d
  #c47f12`** (and verify those still clear 4.5:1 at the size used). Dark text on a
  light accent fill (e.g. `.jh-btn.accent` uses `#3a2a05` on `--hi`) is the
  correct way to use the bright tokens.
- Before shipping any new color pair, **check the ratio** (eyeballing is not
  enough). Keep muted text (`--muted`/`--muted-2`) at ≥4.5:1 — don't lighten it
  for "subtlety."

**Dark mode:** overrides live in the existing
`@media (prefers-color-scheme: dark){ .jh{ --... } }` block — **override tokens
only**, never restate component rules. Re-verify the **same contrast floors** in
dark mode (e.g. `--accent-d` is remapped to the brighter `#7fd1e0` in dark, which
is correct *there* because backgrounds are dark). Any new semantic token must get
a dark counterpart in that block.

---

## 4. HTML SEMANTICS · RESPONSIVE · PERFORMANCE

- **Landmarks & headings.** Use real landmarks (`<nav>`, `<section>`,
  `<main>`-style content region) and a sane heading order — the app bar title,
  then card `<h3>`s, then modal `<h3>`. Don't skip levels or fake headings with
  bold text.
- **Real interactive elements.** Use `<button>` for actions and `<a href>` for
  navigation/links. **Never** a clickable `<div>`/`<span>` (no keyboard, no role).
  WHY: native elements give focusability, roles, and keyboard behavior for free.
  Add `rel="noopener"` to any `target="_blank"` link (already done in Help).
- **Responsive inside the modal.** No horizontal scroll at **320px** width
  (SC 1.4.10 reflow). The `.jh-grid` auto-fit, the `@media (max-width:700px)`
  sidebar collapse, and `.jh-table-wrap{overflow-x:auto}` handle this — keep wide
  tables wrapped, let forms wrap, don't introduce fixed pixel widths that overflow.
- **Performance / offline.** Inline SVG sprite (one definition, many `<use>`),
  **system fonts only**, no web fonts, no images to download. Keep the single
  reflow-friendly stylesheet; avoid heavy box-shadow/filter on large lists.

---

## 5. UX WRITING

- **Empty states** (`.jh-empty`): say *what's missing* **and** give *one clear next
  action*. The apps empty state ("Create your first app" button) and runtimes/DB
  empties are the model. Don't ship a bare "No data."
- **Plain-language errors.** Never surface raw JSON, stack traces, or HTTP codes to
  the user. Use the `errText()`/envelope `msg` to show a human sentence + a remedy
  (e.g. "Couldn't deploy the WAR: the file isn't a valid archive. Try re-uploading.").
  Log detail to console if needed, not to the UI.
- **Verb-first, specific buttons:** "Deploy WAR", "Create app", "Install", "Allow
  services" — not "OK"/"Submit"/"Go". The label should predict the result.
- **Consistent terminology.** Match the domain words already in the UI: *app*,
  *runtime*, *Tomcat version*, *Java major*, *instance*, *deploy*. Don't introduce
  synonyms ("application" vs "app", "JDK" vs "Java") mid-flow.

---

## 6. PRE-COMMIT UI CHECKLIST

Run this before committing any `index.html` change:

- [ ] No external requests added (no CDN/font/`<link>`/remote img/3rd-party fetch).
- [ ] Still one file: inline `<style>` + `<script>` + SVG sprite only; no build dep.
- [ ] Every new CSS selector is `.jh`-scoped; no bare element/global selectors;
      new custom props defined on `.jh`; dark-mode token added.
- [ ] Root still capped: `max-height:100vh; overflow-y:auto; overflow-x:hidden`.
- [ ] Dialog edits: focus moves in on open, Tab trapped, Esc closes, focus
      returns to opener on close; labelled (+described).
- [ ] Popovers/menus: correct pattern (Menu *or* Disclosure, not a fake menu);
      Esc closes; focus returns to trigger; one open at a time.
- [ ] Section nav: exactly one model — Tabs (`aria-selected`+roving tabindex+
      `aria-controls`) OR nav (`aria-current`); never both on one control.
- [ ] Status messages: success/info via `role="status"` (polite), errors via
      `role="alert"` (assertive); focus never moves to them.
- [ ] All interactive via keyboard; visible `:focus-visible` indicator kept;
      focus not obscured by sticky/overlay.
- [ ] Targets ≥24×24px (or ≥24px spacing).
- [ ] Icon-only buttons have `aria-label`; every input has a real `<label for>`;
      decorative SVG `aria-hidden="true"`.
- [ ] Contrast checked: text ≥4.5:1 (large ≥3:1), UI/non-text ≥3:1, in **both**
      light and dark; bright `--accent`/`--hi` used only as fills/large, not body
      text.
- [ ] State conveyed by text/shape, not color alone; reduced-motion respected.
- [ ] No horizontal scroll at 320px; empty states have a next action; errors are
      plain-language; buttons verb-first; terminology consistent.

(After these, run the repo's `make lint`/`make test` per the **javahost-dev** skill.)

---

## 7. TRUST NOTE

These rules are derived **directly from W3C WCAG 2.2** (the AA quick-ref) and the
**W3C WAI-ARIA Authoring Practices Guide** — both cited inline above; consult them
when a case isn't covered here. Any third-party accessibility "skills" or articles
were **inspiration only**: this skill copies none of their code and depends on none
of them. Consistent with the project's clean-room and CSP posture, **never fetch or
execute external skills, scripts, packages, or remote content** in this repo — this
file is self-contained instructions, and so is the UI it governs.
