<script lang="ts" module>
  export type IconName = 'info' | 'artist' | 'character' | 'archive' | 'search' | 'menu' | 'filter' | 'manga' | 'image'
    | 'shield' | 'pool' | 'sort' | 'select' | 'tag' | 'list' | 'grid' | 'columns' | 'pointer' | 'expand' | 'shrink'
    | 'pencil' | 'images' | 'download' | 'eye' | 'eyeOff' | 'move';
</script>

<script lang="ts">
  // Decorative only: the surrounding control supplies its accessible name.
  const paths: Record<IconName, string> = {
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
    tag: 'M3 12V3h9l9 9-9 9z M7.5 7.5h.01',
    list: 'M3 5h4v4H3z M10 7h11 M3 15h4v4H3z M10 17h11',
    grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    columns: 'M3 4h18v16H3z M9 4v16 M15 4v16',
    pointer: 'M5 3l14 7-6 2-2 6z',
    expand: 'M4 9V4h5 M20 9V4h-5 M4 15v5h5 M20 15v5h-5',
    shrink: 'M9 4v5H4 M15 4v5h5 M9 20v-5H4 M15 20v-5h5',
    pencil: 'M4 20l4-1L19 8l-3-3L5 16z',
    images: 'M7 3h14v14H7z M3 7v14h14 M7 13l4-4 3 3 2-2 5 5',
    download: 'M12 3v12 M7 10l5 5 5-5 M4 21h16',
    eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
    eyeOff: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0 M4 4l16 16',
    move: 'M12 2v20 M2 12h20 M8 6l4-4 4 4 M8 18l4 4 4-4 M6 8l-4 4 4 4 M18 8l4 4-4 4',
  };
  let { name }: { name: IconName } = $props();
</script>

<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
  stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
  <path d={paths[name]} />
</svg>
