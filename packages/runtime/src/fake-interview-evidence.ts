export function frontmatterField(content: string, name: string): string | undefined {
  const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/u.exec(content)?.[1];
  if (!frontmatter) return undefined;
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  return new RegExp(`^${escaped}:\\s*(.+?)\\s*$`, "mu").exec(frontmatter)?.[1]?.trim();
}

export function questionIdentity(content: string): string | undefined {
  const title = frontmatterField(content, "title") ?? /^#\s+(.+)$/mu.exec(content)?.[1]?.trim();
  return title?.toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
}

export function experienceIdentity(content: string) {
  const candidate = frontmatterField(content, "candidate")?.toLocaleLowerCase();
  const date = frontmatterField(content, "date");
  const round = frontmatterField(content, "round")?.toLocaleLowerCase();
  return {
    ...(candidate ? { candidate } : {}),
    ...(date ? { date } : {}),
    ...(round ? { round } : {}),
  };
}

export function incrementFrequency(content: string): string | undefined {
  const frequencyMatch = /^frequency:\s*(\d+)\s*$/mu.exec(content);
  const currentFrequency = frequencyMatch ? Number(frequencyMatch[1]) : Number.NaN;
  if (!frequencyMatch || !Number.isSafeInteger(currentFrequency) || currentFrequency < 0) {
    return undefined;
  }
  return content.replace(frequencyMatch[0], `frequency: ${currentFrequency + 1}`);
}
