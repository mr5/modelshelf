/** Formats a date for an `<input type="datetime-local">` in the browser's timezone. */
export function localDateTimeValue(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}
