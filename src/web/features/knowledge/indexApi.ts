import { api } from "../../api";

export type IndexStatus = {
  active_generation_id: string | null;
  status: string;
  content_revision: number;
  active_source_revision: number | null;
  included_version_ids: string[];
  etag: string;
  can_manage_index: boolean;
};

export function switchIndexGeneration(base: string, before: IndexStatus, generationId: string, action: "activate" | "rollback") {
  return api.mutate<IndexStatus>(base + "/active-index", {
    generation_id: generationId,
    action,
    expected_active_id: before.active_generation_id,
    expected_source_revision: before.content_revision,
  }, before.etag, "PUT");
}
