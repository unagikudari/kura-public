export interface MicroTag {
  localizedNames?: Record<string, string>;
  id?: number;
  names: string[];
  category: string;
  usages: number;
  /** post_tag_pin.state — 'present'=施錠(人間確定・タガー上書き不可)。
   *  単一 post 詳細でのみ populate、一覧 payload では undefined。 */
  pinned?: 'present' | 'absent' | null;
}

export interface MicroUser {
  name: string;
}

export interface MicroPool {
  id: number;
  names: string[];
  category: string;
  description?: string | null;
  postCount: number;
}

export interface Post {
  id: number;
  version: number;
  creationTime: string | null;
  lastEditTime: string | null;
  safety: string;
  source: string | null;
  taggingSource: string | null;
  taggingCompletedAt: string | null;
  taggingTagsWritten: string[] | null;
  originalFilename: string | null;
  type: string;
  mimeType: string | null;
  fileSize: number | null;
  canvasWidth: number | null;
  canvasHeight: number | null;
  contentUrl: string;
  thumbnailUrl: string;
  flags: string[];
  tags: MicroTag[];
  user: MicroUser | null;
  score: number;
  ownFavorite: boolean;
  tagCount: number;
  favoriteCount: number;
  commentCount: number;
  noteCount: number;
  pools: MicroPool[];
}

export interface PagedResult<T> {
  query: string;
  offset: number;
  limit: number;
  total: number;
  totalBytes?: number;
  results: T[];
}

export interface TagSuggestion {
  localizedNames?: Record<string, string>;
  id?: number;
  names: string[];
  category: string;
  usages: number;
}

export interface TagDetail {
  localizedNames?: Record<string, string>;
  id: number;
  version: number;
  names: string[];
  category: string;
  categoryColor: string;
  description: string | null;
  creationTime: string | null;
  lastEditTime: string | null;
  usages: number;
  thumbnailUrl?: string | null;
}

export interface TagCategory {
  name: string;
  color: string;
  default: boolean;
}

export interface TagLink {
  id: number;
  tagId: number;
  platform: string;
  url: string;
  handle: string | null;
  uncensored: 0 | 1 | null; // null=unknown, 0=censored, 1=uncensored
  verified: boolean;
  note: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface PoolSource {
  url: string;
  isPrimary: boolean;
  isExternal: boolean;
}

export interface PoolSummary {
  /** Existing artist tags aggregated across pool members, canonical tag IDs. */
  artists?: (MicroTag & { postCount: number })[];
  id: number;
  name: string;
  postCount: number;
  thumbnailUrl: string | null;
  firstPostSource?: string | null;
  // canonical source domain, derived server-side (an extension that knows where the
  // pool came from may name it) — the badge reads this, it does NOT re-detect from URLs.
  domain?: string | null;
  scope?: string | null;
  /** 検証 pool ライフサイクル: null=通常 / 'pending'=検証待ち / 'verified'=検証済み /
   *  'excluded'=検証対象外 (実験用/低品質 — 学習から除外) */
  verifyStatus?: 'pending' | 'verified' | 'excluded' | null;
  taggerSource?: string | null;
  primarySourceUrl?: string | null;
  sources?: PoolSource[];
  /** What installed extensions attached to this pool, keyed by extension id. */
  extensions?: Record<string, Record<string, unknown>>;
  hasCensored?: boolean;      // has a human censor annotation (worked pool) → solid 💢
  hasCensoredTag?: boolean;   // has a censored-tagged post (informational) → faint 💢
}
