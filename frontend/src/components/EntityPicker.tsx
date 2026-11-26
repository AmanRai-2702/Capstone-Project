import { useId, useState } from "react";
import { api } from "../api/client";
import { useAsync, useDebounced } from "../hooks/useAsync";

/** Text box that suggests entities from the repository while typing. The value is an id or a name. */
export function EntityPicker({ repoId, value, onChange, label, placeholder, types }: {
  repoId: string; value: string; onChange: (v: string) => void; label: string; placeholder?: string; types?: string;
}) {
  const listId = useId();
  const [focused, setFocused] = useState(false);
  const term = useDebounced(value, 200);
  const found = useAsync(() => api.entities(repoId, { q: term, type: types, limit: 25 }), [repoId, term, types], focused);
  return (
    <>
      <label className="field">
        {label}
        <input type="text" value={value} list={listId} placeholder={placeholder} onFocus={() => setFocused(true)}
          onChange={(e) => onChange(e.target.value)} autoComplete="off" spellCheck={false} />
      </label>
      {/* outside the label, otherwise every suggestion becomes part of the field's accessible name */}
      <datalist id={listId}>
        {(found.data ?? []).map((e) => <option key={e.id} value={e.id}>{`${e.type} · ${e.name}`}</option>)}
      </datalist>
    </>
  );
}
