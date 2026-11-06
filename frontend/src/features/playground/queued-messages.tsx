// QueuedMessages — the composer's "sends when the reply finishes" list. Each
// entry queued while a run streams can either wait for the run to finish (and
// get auto-sent as one combined turn) or be steered into the live run right
// now via "Steer now" (POST /api/chat/steer).

export interface QueuedEntry { id: string; content: string }

export function QueuedMessages({ items, onSteer, onEdit, onRemove }: {
  items: QueuedEntry[];
  onSteer: (entry: QueuedEntry) => void;
  onEdit: (id: string) => void;
  onRemove: (id: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="mb-2 space-y-1.5 px-1">
      <div className="text-[10px] font-medium uppercase tracking-wide text-mute">
        Queued · sends when the reply finishes
      </div>
      {items.map((entry) => (
        <div
          key={entry.id}
          className="flex items-start gap-2 rounded-2xl border border-pressed bg-softer px-3 py-2"
        >
          <span className="min-w-0 flex-1 line-clamp-2 text-xs text-body">{entry.content}</span>
          <button
            onClick={() => onSteer(entry)}
            title="Inject this into the reply that's streaming right now"
            className="shrink-0 rounded-full border border-primary-line bg-primary-soft px-2.5 py-1 text-[11px] font-medium text-primary-deep hover:bg-primary hover:text-white"
          >
            Steer now
          </button>
          <button
            onClick={() => onEdit(entry.id)}
            title="Edit this message before it sends"
            aria-label="Edit queued message"
            className="shrink-0 text-mute hover:text-ink"
          >
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
            </svg>
          </button>
          <button
            onClick={() => onRemove(entry.id)}
            aria-label="Remove queued message"
            className="shrink-0 text-mute hover:text-ink"
          >✕</button>
        </div>
      ))}
    </div>
  );
}
