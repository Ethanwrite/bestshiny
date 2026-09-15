/**
 * Inline SVG icons for the canvas: 16px, 1.6px round strokes in currentColor.
 * Node types name their icon in the catalogue (`icon`); an unknown name falls
 * back to a neutral square so a new server-side node still renders.
 */

const PATHS = {
  text: '<path d="M3.5 4h9M3.5 8h9M3.5 12h5.5"/>',
  image: '<rect x="2.5" y="3" width="11" height="10" rx="2"/><circle cx="6" cy="6.6" r="1.2"/><path d="m3 12 3.4-3.4a1 1 0 0 1 1.4 0L13 13.6"/>',
  sparkles: '<path d="M8 2.2 9.3 6.1l3.9 1.3-3.9 1.3L8 12.6 6.7 8.7 2.8 7.4l3.9-1.3Z"/><path d="M12.6 11.2l.5 1.4 1.4.5-1.4.5-.5 1.4-.5-1.4-1.4-.5 1.4-.5Z"/>',
  "image-sparkles": '<rect x="2.5" y="4" width="9" height="9" rx="2"/><path d="m3 12 2.6-2.6a1 1 0 0 1 1.4 0L10.5 13"/><path d="M12.8 1.8l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6Z"/>',
  film: '<rect x="2.5" y="3" width="11" height="10" rx="2"/><path d="M5.5 3v10M10.5 3v10M2.5 6h3M2.5 10h3M10.5 6h3M10.5 10h3"/>',
  note: '<path d="M3.5 2.5h9v7l-3 4h-6Z"/><path d="M9.5 13.5v-4h3M5.5 6h5M5.5 8.5h3"/>',
  play: '<path d="M5 3.2v9.6a.6.6 0 0 0 .9.5l7.6-4.8a.6.6 0 0 0 0-1L5.9 2.7a.6.6 0 0 0-.9.5Z" fill="currentColor" stroke="none"/>',
  stop: '<rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor" stroke="none"/>',
  plus: '<path d="M8 3v10M3 8h10"/>',
  minus: '<path d="M3 8h10"/>',
  fit: '<path d="M2.5 6V3.5a1 1 0 0 1 1-1H6M10 2.5h2.5a1 1 0 0 1 1 1V6M13.5 10v2.5a1 1 0 0 1-1 1H10M6 13.5H3.5a1 1 0 0 1-1-1V10"/>',
  more: '<circle cx="3.5" cy="8" r="1.1" fill="currentColor" stroke="none"/><circle cx="8" cy="8" r="1.1" fill="currentColor" stroke="none"/><circle cx="12.5" cy="8" r="1.1" fill="currentColor" stroke="none"/>',
  trash: '<path d="M2.8 4.3h10.4M6.3 4.3V2.8h3.4v1.5M4.2 4.3l.7 9h6.2l.7-9M6.8 6.8v4M9.2 6.8v4"/>',
  copy: '<rect x="5.5" y="5.5" width="8" height="8" rx="1.6"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/>',
  duplicate: '<rect x="2.5" y="2.5" width="8" height="8" rx="1.6"/><path d="M13.5 6v6.5a1 1 0 0 1-1 1H6M9 5v3M7.5 6.5h3"/>',
  check: '<path d="m3.2 8.4 3 3 6.6-6.8"/>',
  x: '<path d="m4 4 8 8M12 4l-8 8"/>',
  alert: '<path d="M8 2.6 14 13H2Z"/><path d="M8 6.5v3M8 11.3v.2"/>',
  plug: '<path d="M6 2.5v3M10 2.5v3M4.5 5.5h7v2.5a3.5 3.5 0 0 1-7 0Z"/><path d="M8 11.5v2"/>',
  key: '<circle cx="5.5" cy="10.5" r="3"/><path d="m7.6 8.4 5.9-5.9M11.3 4.7l1.6 1.6M9.8 6.2l1.3 1.3"/>',
  chevron: '<path d="m4 6 4 4 4-4"/>',
  "chevron-left": '<path d="m10 4-4 4 4 4"/>',
  "chevron-right": '<path d="m6 4 4 4-4 4"/>',
  search: '<circle cx="7" cy="7" r="4.5"/><path d="m10.4 10.4 3.1 3.1"/>',
  upload: '<path d="M8 10.5V3M5 6l3-3 3 3M3 10.5v1.8a1.2 1.2 0 0 0 1.2 1.2h7.6a1.2 1.2 0 0 0 1.2-1.2v-1.8"/>',
  refresh: '<path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.8v2.8h-2.8"/>',
  panel: '<rect x="2.5" y="3" width="11" height="10" rx="2"/><path d="M6.5 3v10"/>',
  "panel-right": '<rect x="2.5" y="3" width="11" height="10" rx="2"/><path d="M9.5 3v10"/>',
  grid: '<rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1"/><rect x="9" y="2.5" width="4.5" height="4.5" rx="1"/><rect x="2.5" y="9" width="4.5" height="4.5" rx="1"/><rect x="9" y="9" width="4.5" height="4.5" rx="1"/>',
  clock: '<circle cx="8" cy="8" r="5.5"/><path d="M8 5v3.2l2 1.3"/>',
  eye: '<path d="M1.8 8S4 3.8 8 3.8 14.2 8 14.2 8 12 12.2 8 12.2 1.8 8 1.8 8Z"/><circle cx="8" cy="8" r="1.8"/>',
  "eye-off": '<path d="M2.5 2.5l11 11M6.6 4a5.8 5.8 0 0 1 1.4-.2c4 0 6.2 4.2 6.2 4.2a10 10 0 0 1-1.7 2.2M4.2 5.2A10 10 0 0 0 1.8 8S4 12.2 8 12.2a5.6 5.6 0 0 0 2.7-.7"/>',
  pencil: '<path d="M10.8 2.7a1.4 1.4 0 0 1 2 2L5.6 11.9 2.8 12.6l.7-2.8Z"/>',
  expand: '<path d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9"/>',
  link: '<path d="M6.8 9.2a2.8 2.8 0 0 0 4 0l2-2a2.8 2.8 0 0 0-4-4l-.8.8M9.2 6.8a2.8 2.8 0 0 0-4 0l-2 2a2.8 2.8 0 0 0 4 4l.8-.8"/>',
  user: '<circle cx="8" cy="5.5" r="2.8"/><path d="M2.8 13.5a5.2 5.2 0 0 1 10.4 0"/>',
  logout: '<path d="M6.5 13.5h-3a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h3M10.5 11l3-3-3-3M13.5 8H6"/>',
  studio: '<rect x="2.5" y="3" width="11" height="8" rx="1.6"/><path d="M5.5 13.5h5M8 11v2.5"/>',
  shield: '<path d="M8 2.2 13 4v3.8c0 3-2.2 5.2-5 6-2.8-.8-5-3-5-6V4Z"/>',
  hand: '<path d="M5.5 8V3.8a1 1 0 0 1 2 0V7m0-3.8a1 1 0 0 1 2 0V7m0-2.6a1 1 0 0 1 2 0V9a4.5 4.5 0 0 1-4.5 4.5A4 4 0 0 1 3.4 11L2 8.3a1 1 0 0 1 1.7-1l1.8 2"/>',
  cursor: '<path d="M3.5 2.5 12 7.3l-3.8 1-1.6 3.7Z"/>',
  keyboard: '<rect x="1.8" y="4" width="12.4" height="8" rx="1.6"/><path d="M4.5 6.5h.1M7 6.5h.1M9.5 6.5h.1M12 6.5h-.4M5 9.5h6"/>',
  info: '<circle cx="8" cy="8" r="5.8"/><path d="M8 7.3v3.6M8 5.2v.2"/>',
  square: '<rect x="3" y="3" width="10" height="10" rx="2"/>',
  undo: '<path d="M5.5 3 2.5 6l3 3M2.8 6H9a4 4 0 0 1 0 8H6"/>',
  redo: '<path d="m10.5 3 3 3-3 3M13.2 6H7a4 4 0 0 0 0 8h3"/>',
  coins: '<ellipse cx="8" cy="4.8" rx="4.5" ry="2"/><path d="M3.5 4.8v3.2c0 1.1 2 2 4.5 2s4.5-.9 4.5-2V4.8M3.5 8v3.2c0 1.1 2 2 4.5 2s4.5-.9 4.5-2V8"/>',
};

export function icon(name, { size = 16, className = "" } = {}) {
  const body = PATHS[name] || PATHS.square;
  return `<svg class="cv-icon${className ? ` ${className}` : ""}" width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
}

export function hasIcon(name) {
  return Object.prototype.hasOwnProperty.call(PATHS, name);
}
