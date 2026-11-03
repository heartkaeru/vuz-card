export type University = {
  id: string;
  name: string;
  city: string;
  tone: "mint" | "blue" | "yellow";
  draft?: boolean;
  website?: string;
  backendId?: string;
  version?: number;
};

export type Section = "about" | "contacts" | "projects" | "events";
export type View = "overview" | "universities" | "events" | "reports";

export const initialUniversities: University[] = [
  { id: "mgu", name: "МГУ им. М.В. Ломоносова", city: "Москва", tone: "mint" },
  { id: "bmstu", name: "МГТУ им. Н.Э. Баумана", city: "Москва", tone: "blue" },
  { id: "mipt", name: "МФТИ", city: "Долгопрудный, Московская область", tone: "blue" },
  { id: "spbgu", name: "СПбГУ", city: "Санкт-Петербург", tone: "yellow" },
  { id: "mephi", name: "НИЯУ МИФИ", city: "Москва", tone: "mint" },
  { id: "hse", name: "НИУ ВШЭ", city: "Москва", tone: "mint" },
];

export const sampleEvents = [
  { title: "День открытых дверей", description: "Знакомство с программами и инфраструктурой кампуса." },
  { title: "Карьерный форум", description: "Встречи студентов с представителями работодателей." },
  { title: "Неделя науки", description: "Лекции, лабораторные экскурсии и презентации проектов." },
  { title: "Ярмарка стажировок", description: "Возможности практики и первых профессиональных проектов." },
  { title: "Фестиваль студенческих проектов", description: "Демонстрация инициатив и командных разработок." },
];

export const sampleProjects = [
  "Цифровые образовательные сервисы",
  "Прикладные исследования и НИОКР",
  "Карьерные программы и стажировки",
  "Студенческие предпринимательские инициативы",
  "Программы дополнительного образования",
];

export const studyAreas = [
  "Информационные технологии и программная инженерия",
  "Инженерные и прикладные науки",
  "Естественные науки и исследования",
  "Экономика, управление и аналитика",
  "Гуманитарные и социальные направления",
];

export const savedReports = [
  { id: "report-pptx", format: "PPTX", name: "Карточка образовательной организации", date: "20.10.2026" },
  { id: "report-docx", format: "DOCX", name: "Аналитическая справка к встрече", date: "18.10.2026" },
  { id: "report-xlsx", format: "XLSX", name: "Сводка контактов и проектов", date: "15.10.2026" },
] as const;

export function createUniversity(name: string, city: string): University {
  const cleanName = name.trim();
  const cleanCity = city.trim() || "Регион не указан";
  const slug = cleanName
    .toLocaleLowerCase("ru-RU")
    .replace(/[^а-яёa-z0-9]+/gi, "-")
    .replace(/^-|-$/g, "");

  return {
    id: `${slug || "new-university"}-${Date.now()}`,
    name: cleanName,
    city: cleanCity,
    tone: "mint",
    draft: true,
  };
}

export function initialsFor(name: string) {
  const words = name.replace(/[«»“”]/g, "").trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "ВУ";
  const first = words[0].replace(/[^А-ЯЁA-Z]/gi, "").slice(0, 1);
  const second = words.length > 1 ? words[1].replace(/[^А-ЯЁA-Z]/gi, "").slice(0, 1) : "";
  return `${first}${second}`.toUpperCase();
}
