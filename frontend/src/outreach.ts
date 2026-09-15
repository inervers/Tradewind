export function internationalPhone(phone?: string, country?: string): string {
  if (!phone) return "";
  const raw = phone.trim();
  const digits = raw.replace(/\D/g, "");
  let normalized = raw;
  if (raw.startsWith("+")) normalized = `+${digits}`;
  else if (raw.startsWith("00")) normalized = `+${digits.slice(2)}`;

  const place = (country || "").toLowerCase();
  const isHongKong = /香港|hong\s*kong|\bhk\b/.test(place);
  const isMainland = /中国大陆|中國大陸|大陆|大陸|mainland|\bchina\b/.test(place);
  if (!normalized.startsWith("+") && (isHongKong || digits.startsWith("852")) && (digits.length === 8 || digits.length === 11)) {
    normalized = `+${digits.startsWith("852") ? digits : `852${digits}`}`;
  }
  if (!normalized.startsWith("+") && isMainland && /^1\d{10}$/.test(digits)) normalized = `+86${digits}`;
  if (!normalized.startsWith("+") && digits.startsWith("86") && digits.length === 13) normalized = `+${digits}`;

  if (/^\+852\d{8}$/.test(normalized)) return `+852 ${normalized.slice(4)}`;
  if (/^\+86\d{11}$/.test(normalized)) return `+86 ${normalized.slice(3)}`;
  return normalized;
}

export function waLink(phone?: string, country?: string): string {
  if (!phone) return "";
  const normalized = internationalPhone(phone, country);
  const digits = normalized.replace(/\D/g, "");
  return normalized.startsWith("+") && digits.length >= 10 ? `https://wa.me/${digits}` : "";
}
