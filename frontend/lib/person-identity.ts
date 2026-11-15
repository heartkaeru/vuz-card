const patronymicSuffixes = ["ович", "евич", "овна", "евна", "ична", "инична", "ич"];

export type PersonNameEntry = { name: string; department?: string | null };

function normalizePart(value: string) {
  return value.normalize("NFKC").toLocaleLowerCase("ru-RU").replace(/ё/g, "е").replace(/\s+/g, " ").trim();
}

export function personNameParts(value: string) {
  const tokens = normalizePart(value).match(/[\p{L}\p{M}]+(?:-[\p{L}\p{M}]+)*/gu) ?? [];
  const patronymicIndex = tokens.findIndex((token) => tokens.length > 2
    && patronymicSuffixes.some((suffix) => token.endsWith(suffix)));
  const patronymic = patronymicIndex >= 0 ? tokens.splice(patronymicIndex, 1)[0] : "";
  return { core: tokens.sort().join(" ") || normalizePart(value), patronymic };
}

function identityScope(core: string, department?: string | null) {
  return `${core}\u0000${normalizePart(department ?? "")}`;
}

export function buildPersonAliases(people: PersonNameEntry[]) {
  const patronymics = new Map<string, Set<string>>();
  for (const person of people) {
    const { core, patronymic } = personNameParts(person.name);
    if (!patronymic) continue;
    const scope = identityScope(core, person.department);
    const known = patronymics.get(scope) ?? new Set<string>();
    known.add(patronymic);
    patronymics.set(scope, known);
  }
  const aliases = new Map<string, string>();
  for (const [scope, known] of patronymics) aliases.set(scope, known.size === 1 ? [...known][0] : "");
  return aliases;
}

export function personIdentityKey(name: string, department?: string | null, aliases?: Map<string, string>) {
  const { core, patronymic } = personNameParts(name);
  const canonicalPatronymic = aliases?.get(identityScope(core, department)) || patronymic;
  return `${core}\u0000${normalizePart(department ?? "")}\u0000${canonicalPatronymic}`;
}
