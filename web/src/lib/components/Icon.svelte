<script lang="ts">
  // Decorative only: the surrounding control supplies its accessible name.
  const paths = {
    info: 'M12 8h.01 M11 12h1v5 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
    artist: 'M4 20l4-1L20 7l-3-3L5 16z M14 7l3 3',
    character: 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M4 21v-2a8 8 0 0 1 16 0v2',
    archive: 'M4 7h16v13H4z M3 3h18v4H3z M9 11h6',
    search: 'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
    menu: 'M4 7h16 M4 12h16 M4 17h16',
    filter: 'M4 7h9 M17 7h3 M4 17h3 M11 17h9 M13 4v6 M7 14v6',
    manga: 'M3 4h18v16H3z M12 4v16 M6 8h3 M15 8h3',
    image: 'M3 3h18v18H3z M3 16l5-5 4 4 3-3 6 6 M15 7h.01',
    shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z',
    pool: 'M3 8h18v13H3z M6 5h12 M9 2h6',
    sort: 'M8 4v16 M4 8l4-4 4 4 M16 20V4 M12 16l4 4 4-4',
    select: 'M3 5l2 2 4-4 M12 5h9 M3 13l2 2 4-4 M12 13h9 M12 21h9',
  };
  let { name }: { name: keyof typeof paths } = $props();
</script>

<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
  stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
  <path d={paths[name]} />
</svg>
