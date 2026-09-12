/** Wire contracts shared by workspace lists and Svelte stores. */
export type WorkspaceUser = { id: string; name: string; email?: string; profile_image_url?: string; role?: string };

export type WorkspaceTool = {
    id: string;
    user_id?: string | null;
    name: string;
    meta: { description?: string | null; manifest?: Record<string, unknown> | null; has_user_valves?: boolean };
    has_user_valves?: boolean;
    authenticated?: boolean;
    specs?: Array<{ name?: string; function?: { name?: string } }>;
    updated_at: number;
    created_at: number;
    user?: WorkspaceUser | null;
};

export type WorkspaceSkill = {
    id: string; user_id: string; name: string; description?: string | null;
    meta: { tags?: string[] | null }; is_active: boolean;
    updated_at: number; created_at: number; user?: WorkspaceUser | null;
};

export type WorkspaceFunction = {
    id: string; user_id?: string | null; name: string; type: string;
    meta: { description?: string | null; manifest?: Record<string, unknown> | null };
    is_active: boolean; is_global: boolean; updated_at: number; created_at: number;
    user?: WorkspaceUser | null;
};

export type FolderSummary = {
    id: string; name: string; parent_id?: string | null; user_id?: string;
    permission?: string;
    is_expanded: boolean; unread_count?: number; created_at: number; updated_at: number;
    meta?: { icon?: string | null; background_image_url?: string | null } | null;
    data?: { model_ids?: string[]; system_prompt?: string; files?: unknown[] } | null;
};

export type NoteSummary = {
    id: string; title: string; data: Record<string, unknown> | null;
    is_pinned?: boolean | null; updated_at: number; created_at: number;
    user?: WorkspaceUser | null;
};

export type ChatTag = { id: string; name: string; user_id?: string; data?: Record<string, unknown> | null };
