import { useCallback, useEffect, useState } from "react";

interface TeamSettingsProps {
  open: boolean;
  onClose: () => void;
  apiBaseUrl: string;
  getToken: () => Promise<string | null>;
}

interface Team {
  _id: string;
  name: string;
  slug: string;
  github_configured: boolean;
  github_pat_configured_at: string | null;
  can_manage: boolean;
}

interface Member {
  clerk_id: string;
  email: string;
  full_name?: string;
  role: "member" | "manager";
}

function formatConfiguredAt(value: string | null) {
  if (!value) return "not configured";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : `configured ${date.toLocaleString()}`;
}

// Auto-provisioned accounts (backend/utils/isolation_auth.py) fall back to the generic
// "errAgent Operator" full_name whenever Clerk doesn't hand back a real name — which makes
// every such member look identical in a roster. Prefer the email (still per-account, even
// when it's Clerk's own user_<id>@example.com placeholder) whenever the name isn't distinguishing.
function memberDisplayName(member: Member): string {
  const hasRealName = member.full_name && member.full_name !== "errAgent Operator";
  if (hasRealName && member.email) return `${member.full_name} (${member.email})`;
  return member.email || member.full_name || member.clerk_id;
}

export function TeamSettings({ open, onClose, apiBaseUrl, getToken }: TeamSettingsProps) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [selectedSlug, setSelectedSlug] = useState("");
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [patInput, setPatInput] = useState("");
  const [savingPat, setSavingPat] = useState(false);

  const [newMemberId, setNewMemberId] = useState("");
  const [newMemberRole, setNewMemberRole] = useState<"member" | "manager">("member");
  const [addingMember, setAddingMember] = useState(false);

  const authedFetch = useCallback(
    async (path: string, init?: RequestInit) => {
      const token = await getToken();
      const response = await fetch(`${apiBaseUrl}${path}`, {
        ...init,
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
          ...(init?.headers || {}),
        },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `Request failed: ${response.status}`);
      return body;
    },
    [apiBaseUrl, getToken],
  );

  const loadTeams = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const body = await authedFetch("/admin/teams");
      const nextTeams = (body.teams || []) as Team[];
      setTeams(nextTeams);
      setSelectedSlug((current) => (nextTeams.some((t) => t.slug === current) ? current : nextTeams[0]?.slug || ""));
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [authedFetch]);

  const loadMembers = useCallback(
    async (slug: string) => {
      if (!slug) {
        setMembers([]);
        return;
      }
      try {
        const body = await authedFetch(`/admin/teams/${slug}/members`);
        setMembers((body.members || []) as Member[]);
      } catch (err) {
        setError(String(err));
      }
    },
    [authedFetch],
  );

  useEffect(() => {
    if (!open) return;
    setNotice("");
    setError("");
    void loadTeams();
  }, [open, loadTeams]);

  useEffect(() => {
    if (!open || !selectedSlug) return;
    void loadMembers(selectedSlug);
    setPatInput("");
  }, [open, selectedSlug, loadMembers]);

  if (!open) return null;

  const selectedTeam = teams.find((t) => t.slug === selectedSlug) || null;

  const handleSetPat = async () => {
    if (!selectedTeam || !patInput.trim()) return;
    setSavingPat(true);
    setError("");
    setNotice("");
    try {
      await authedFetch(`/teams/${selectedTeam.slug}/github-credential`, {
        method: "PUT",
        body: JSON.stringify({ pat: patInput.trim() }),
      });
      setPatInput("");
      setNotice("GitHub credential saved.");
      await loadTeams();
    } catch (err) {
      setError(String(err));
    } finally {
      setSavingPat(false);
    }
  };

  const handleClearPat = async () => {
    if (!selectedTeam) return;
    setSavingPat(true);
    setError("");
    setNotice("");
    try {
      await authedFetch(`/teams/${selectedTeam.slug}/github-credential`, { method: "DELETE" });
      setNotice("GitHub credential cleared.");
      await loadTeams();
    } catch (err) {
      setError(String(err));
    } finally {
      setSavingPat(false);
    }
  };

  const handleAddMember = async () => {
    if (!selectedTeam || !newMemberId.trim()) return;
    setAddingMember(true);
    setError("");
    setNotice("");
    try {
      const isEmail = newMemberId.includes("@");
      await authedFetch(`/admin/teams/${selectedTeam.slug}/members`, {
        method: "POST",
        body: JSON.stringify({
          clerk_id: isEmail ? null : newMemberId.trim(),
          email: isEmail ? newMemberId.trim() : null,
          role: newMemberRole,
        }),
      });
      setNewMemberId("");
      setNewMemberRole("member");
      setNotice("Member added.");
      await loadMembers(selectedTeam.slug);
    } catch (err) {
      setError(String(err));
    } finally {
      setAddingMember(false);
    }
  };

  const handleSetRole = async (member: Member, role: "member" | "manager") => {
    if (!selectedTeam) return;
    setError("");
    setNotice("");
    try {
      await authedFetch(`/admin/teams/${selectedTeam.slug}/members/${member.clerk_id}`, {
        method: "PUT",
        body: JSON.stringify({ role }),
      });
      await loadMembers(selectedTeam.slug);
    } catch (err) {
      setError(String(err));
    }
  };

  const handleRemoveMember = async (member: Member) => {
    if (!selectedTeam) return;
    setError("");
    setNotice("");
    try {
      await authedFetch(`/admin/teams/${selectedTeam.slug}/members/${member.clerk_id}`, { method: "DELETE" });
      await loadMembers(selectedTeam.slug);
    } catch (err) {
      setError(String(err));
    }
  };

  return (
    <div className="console-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <section className="live-console" role="dialog" aria-modal="true" aria-labelledby="team-settings-title">
        <header className="console-header">
          <div>
            <p className="eyebrow">Team administration</p>
            <h2 id="team-settings-title">Team Settings</h2>
          </div>
          <div className="console-header-actions">
            <button type="button" className="console-icon-button" onClick={onClose} title="Close" aria-label="Close">
              ×
            </button>
          </div>
        </header>

        <div className="console-toolbar">
          <label>
            <span>Team</span>
            <select value={selectedSlug} onChange={(e) => setSelectedSlug(e.target.value)} disabled={loading || teams.length === 0}>
              {teams.length === 0 ? (
                <option value="">{loading ? "Loading teams…" : "No teams available"}</option>
              ) : (
                teams.map((team) => (
                  <option key={team.slug} value={team.slug}>
                    {team.name} ({team.slug}){team.can_manage ? "" : " — view only"}
                  </option>
                ))
              )}
            </select>
          </label>
        </div>

        <div className="console-viewport team-settings-body">
          {error && <div className="console-error">{error}</div>}
          {notice && !error && <div className="team-settings-notice">{notice}</div>}

          {selectedTeam && (
            <>
              <section className="team-settings-section">
                <h3>GitHub credential</h3>
                <p className="team-settings-status">
                  {selectedTeam.github_configured ? "Configured" : "Not configured"} · {formatConfiguredAt(selectedTeam.github_pat_configured_at)}
                </p>
                {selectedTeam.can_manage ? (
                  <div className="team-settings-row">
                    <input
                      className="team-settings-input"
                      type="password"
                      placeholder="ghp_…"
                      value={patInput}
                      onChange={(e) => setPatInput(e.target.value)}
                    />
                    <button type="button" onClick={handleSetPat} disabled={savingPat || !patInput.trim()}>
                      {selectedTeam.github_configured ? "Rotate" : "Set"}
                    </button>
                    {selectedTeam.github_configured && (
                      <button type="button" onClick={handleClearPat} disabled={savingPat}>
                        Clear
                      </button>
                    )}
                  </div>
                ) : (
                  <p className="team-settings-hint">Only a team manager can set or change this token — the value itself is never shown here.</p>
                )}
              </section>

              <section className="team-settings-section">
                <h3>Members ({members.length})</h3>
                <ul className="team-settings-member-list">
                  {members.map((member) => (
                    <li key={member.clerk_id} className="team-settings-member-row">
                      <span className="team-settings-member-name">{memberDisplayName(member)}</span>
                      <span className={`team-settings-role-badge team-settings-role-${member.role}`}>{member.role}</span>
                      {selectedTeam.can_manage && (
                        <span className="team-settings-member-actions">
                          <button
                            type="button"
                            onClick={() => handleSetRole(member, member.role === "manager" ? "member" : "manager")}
                          >
                            {member.role === "manager" ? "Demote" : "Promote"}
                          </button>
                          <button type="button" onClick={() => handleRemoveMember(member)}>
                            Remove
                          </button>
                        </span>
                      )}
                    </li>
                  ))}
                </ul>

                {selectedTeam.can_manage && (
                  <div className="team-settings-row">
                    <input
                      className="team-settings-input"
                      type="text"
                      placeholder="clerk_id or email"
                      value={newMemberId}
                      onChange={(e) => setNewMemberId(e.target.value)}
                    />
                    <select value={newMemberRole} onChange={(e) => setNewMemberRole(e.target.value as "member" | "manager")}>
                      <option value="member">Member</option>
                      <option value="manager">Manager</option>
                    </select>
                    <button type="button" onClick={handleAddMember} disabled={addingMember || !newMemberId.trim()}>
                      Add
                    </button>
                  </div>
                )}
              </section>
            </>
          )}

          {!selectedTeam && !loading && <div className="console-empty">You don't belong to any team yet.</div>}
        </div>

        <footer className="console-footer">
          <span>{selectedTeam ? `Team: ${selectedTeam.name}` : "No team selected"}</span>
        </footer>
      </section>
    </div>
  );
}
