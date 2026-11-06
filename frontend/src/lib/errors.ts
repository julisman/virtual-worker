/**
 * Normalize an unknown thrown value to a display string. Replaces the
 * `e instanceof Error ? e.message : String(e)` idiom repeated across features.
 */
export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
