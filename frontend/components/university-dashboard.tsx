"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ResearchPanel, type ResearchHandle } from "@/components/research-panel";
import { api, post, errorText, type ApiCard, type SavedMaterial } from "@/lib/api";
import { AgentChat } from "@/components/agent-chat";
import { AgentSphere } from "@/components/agent-sphere";
import { Icon, type IconName } from "@/components/icon";
import {
  initialUniversities,
  initialsFor,
  sampleEvents,
  sampleProjects,
  studyAreas,
  type Section,
  type University,
  type View,
} from "@/data/universities";

const navigation: { key: View; label: string; icon: IconName }[] = [
  { key: "universities", label: "ВУЗы", icon: "university" },
  { key: "overview", label: "Обзор", icon: "overview" },
  { key: "events", label: "Мероприятия", icon: "calendar" },
  { key: "reports", label: "Отчёты", icon: "file" },
];

const tabs: { key: Section; label: string }[] = [
  { key: "about", label: "О вузе" },
  { key: "contacts", label: "Руководство и контакты" },
  { key: "projects", label: "Проекты" },
  { key: "events", label: "Мероприятия" },
];

type Contact = { name: string; role: string; phone: string; email: string };

export default function UniversityDashboard() {
  const [view, setView] = useState<View>("universities");
  const [section, setSection] = useState<Section>("about");
  const [hoveredSection, setHoveredSection] = useState<Section | null>(null);
  const [tabIndicator, setTabIndicator] = useState({ left: 0, width: 0 });
  const tabRefs = useRef<Partial<Record<Section, HTMLButtonElement>>>({});
  const [universities, setUniversities] = useState(initialUniversities);
  const [selectedId, setSelectedId] = useState(initialUniversities[0].id);
  const [hasChosenUniversity, setHasChosenUniversity] = useState(false);
  const [universitySearch, setUniversitySearch] = useState("");
  const [globalSearch, setGlobalSearch] = useState("");
  const [regionFilter, setRegionFilter] = useState("Все регионы");
  const [chatOpen, setChatOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createCity, setCreateCity] = useState("");
  const [createWebsite, setCreateWebsite] = useState("");
  const [creating, setCreating] = useState(false);
  const researchRef = useRef<ResearchHandle>(null);
  const [contacts, setContacts] = useState<Record<string, Contact[]>>({});
  const [contactOpen, setContactOpen] = useState(false);
  const [contactDraft, setContactDraft] = useState<Contact>({ name: "", role: "", phone: "", email: "" });
  const [reportSearch, setReportSearch] = useState("");
  const [reports, setReports] = useState<SavedMaterial[]>([]);
  const [reportFilter, setReportFilter] = useState("Все форматы");
  const [reportFilterOpen, setReportFilterOpen] = useState(false);
  const [toast, setToast] = useState("");

  const selectedUniversity = universities.find((university) => university.id === selectedId) ?? universities[0];
  const regions = useMemo(() => [...new Set(universities.map((university) => university.city))], [universities]);
  const filteredUniversities = universities.filter((university) => {
    const matchesQuery = `${university.name} ${university.city}`.toLocaleLowerCase("ru-RU").includes(universitySearch.toLocaleLowerCase("ru-RU").trim());
    const matchesRegion = regionFilter === "Все регионы" || university.city === regionFilter;
    return matchesQuery && matchesRegion;
  });
  const universityContacts = contacts[selectedId] ?? [];
  const filteredReports = reports.filter((report) => {
    const matchesQuery = `${report.name} ${report.format}`.toLocaleLowerCase("ru-RU").includes(reportSearch.toLocaleLowerCase("ru-RU").trim());
    return matchesQuery && (reportFilter === "Все форматы" || report.format === reportFilter);
  });
  const indicatorSection = hoveredSection ?? section;

  useEffect(() => {
    if (view === "reports") api<SavedMaterial[]>("/materials").then(setReports)
      .catch((failure) => setToast(errorText(failure)));
  }, [view]);

  useEffect(() => {
    api<ApiCard[]>("/cards").then((cards) => {
      if (!cards.length) return;
      const persisted: University[] = cards.map((card) => ({ ...card, id: card.id, backendId: card.id,
        tone: "mint", draft: true }));
      setUniversities([...persisted, ...initialUniversities.filter((item) => !cards.some((card) => card.name === item.name))]);
    }).catch(() => setToast("Сервер недоступен. Запустите backend, чтобы сохранять карточки и собирать факты."));
  }, []);

  useLayoutEffect(() => {
    const updateIndicator = () => {
      const tab = tabRefs.current[indicatorSection];
      if (!tab) return;
      setTabIndicator({ left: tab.offsetLeft, width: tab.offsetWidth });
    };

    updateIndicator();
    window.addEventListener("resize", updateIndicator);
    return () => window.removeEventListener("resize", updateIndicator);
  }, [indicatorSection]);

  const notify = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 3200);
  };

  const navigate = (nextView: View) => {
    if (nextView === "overview" && !hasChosenUniversity) return;
    setView(nextView);
    if (nextView === "overview") setSection("about");
    if (nextView === "events") setSection("events");
    setReportFilterOpen(false);
  };

  const openUniversity = (university: University) => {
    setSelectedId(university.id);
    setHasChosenUniversity(true);
    setSection("about");
    setView("overview");
  };

  const handleGlobalSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setUniversitySearch(globalSearch);
    setRegionFilter("Все регионы");
    setView("universities");
  };

  const openCreate = (suggestedName = "") => {
    setCreateName(suggestedName);
    setCreateCity("");
    setCreateWebsite("");
    setCreateOpen(true);
  };

  const handleCreateUniversity = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!createName.trim()) return;
    setCreating(true);
    try {
    const card = await post<ApiCard>("/cards", { name: createName, city: createCity, website: createWebsite });
    const university: University = { ...card, backendId: card.id, tone: "mint", draft: true };
    setUniversities((current) => [university, ...current]);
    setSelectedId(university.id);
    setHasChosenUniversity(true);
    setCreateOpen(false);
    setUniversitySearch("");
    setGlobalSearch("");
    setRegionFilter("Все регионы");
    setSection("about");
    setView("overview");
    notify("Создан черновик карточки. Факты можно добавить после проверки источников.");
    } catch (failure) { notify(errorText(failure)); }
    finally { setCreating(false); }
  };

  const handleCreateContact = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!contactDraft.name.trim()) return;
    setContacts((current) => ({
      ...current,
      [selectedId]: [...(current[selectedId] ?? []), contactDraft],
    }));
    setContactDraft({ name: "", role: "", phone: "", email: "" });
    setContactOpen(false);
    notify("Контакт добавлен в черновик карточки.");
  };

  const onChatClose = () => setChatOpen(false);

  return (
    <div className="workspace">
      <aside className="sidebar">
        <div className="sidebar__top">
          <p className="sidebar__eyebrow">РАБОЧЕЕ ПРОСТРАНСТВО</p>
          <nav className="sidebar__nav" aria-label="Основная навигация">
            {navigation.map((item) => (
              <button
                className={`nav-link${view === item.key ? " nav-link--active" : ""}`}
                type="button"
                key={item.key}
                disabled={item.key === "overview" && !hasChosenUniversity}
                onClick={() => navigate(item.key)}
                aria-current={view === item.key ? "page" : undefined}
                aria-disabled={item.key === "overview" && !hasChosenUniversity}
              >
                <span className="nav-link__icon"><Icon name={item.icon} size={18} /></span>
                <span>{item.label}</span>
                {view === item.key && <span className="nav-link__check"><Icon name="check" size={12} /></span>}
              </button>
            ))}
          </nav>
        </div>
        <button className="user-card" type="button" onClick={() => notify("Профиль сотрудника") }>
          <span className="user-card__avatar">ИИ</span>
          <span className="user-card__name">Сотрудник</span>
          <Icon className="user-card__dots" name="dots" size={20} />
        </button>
      </aside>

      <main className="main-area" id="top">
        <div className="mobile-topbar">
          <button className="mobile-agent-button" type="button" onClick={() => setChatOpen(true)}>
            <Icon name="spark" size={18} /> Агент
          </button>
        </div>

        {view === "universities" && (
          <section className="page-section university-page page-enter" key="universities" aria-label="Список вузов">
            <div className="university-toolbar">
              <label className="search-field search-field--large">
                <Icon name="search" size={24} />
                <input
                  value={universitySearch}
                  onChange={(event) => setUniversitySearch(event.target.value)}
                  placeholder="Поиск по ВУЗам"
                  aria-label="Поиск по вузам"
                />
                {universitySearch && <button className="search-clear" type="button" onClick={() => setUniversitySearch("")} aria-label="Очистить поиск"><Icon name="close" size={16} /></button>}
              </label>
              <label className="select-field" aria-label="Фильтр по региону">
                <select value={regionFilter} onChange={(event) => setRegionFilter(event.target.value)}>
                  <option>Все регионы</option>
                  {regions.map((region) => <option key={region}>{region}</option>)}
                </select>
                <Icon name="chevron" size={18} />
              </label>
              <button className="icon-button toolbar-add" type="button" onClick={() => openCreate()} aria-label="Добавить вуз" title="Добавить вуз">
                <Icon name="plus" size={20} />
              </button>
            </div>

            {filteredUniversities.length > 0 ? (
              <div className="university-grid">
                {filteredUniversities.map((university, index) => (
                  <button
                    className="university-card"
                    type="button"
                    key={university.id}
                    onClick={() => openUniversity(university)}
                    style={{ animationDelay: `${index * 45}ms` }}
                  >
                    <span className={`university-card__art university-card__art--${university.tone}`}>
                      <span className="university-card__monogram">{initialsFor(university.name)}</span>
                      {university.draft && <span className="university-card__draft">Черновик</span>}
                    </span>
                    <span className="university-card__body">
                      <span className="university-card__name">{university.name}</span>
                      <span className="university-card__city"><Icon name="pin" size={13} /> {university.city}</span>
                    </span>
                    <span className="university-card__arrow"><Icon name="arrow" size={17} /></span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="empty-state empty-state--search">
                <span className="empty-state__icon"><Icon name="university" size={24} /></span>
                <h2>Такого вуза пока нет в базе</h2>
                <p>Создайте черновик — название и регион попадут в карточку, а сведения можно добавить после проверки источников.</p>
                <button className="button button--primary" type="button" onClick={() => openCreate(universitySearch)}>
                  <Icon name="plus" size={17} /> Создать карточку
                </button>
              </div>
            )}
            <div className="page-caption"><span className="caption-dot" /> Карточки организаций · {universities.length}</div>
          </section>
        )}

        {view === "overview" && (
          <section className="page-section profile-page page-enter" key={`profile-${selectedUniversity.id}`} aria-label={`Карточка: ${selectedUniversity.name}`}>
            <div className="profile-topbar">
              <div className="breadcrumbs"><button type="button" onClick={() => navigate("universities")}>ВУЗы</button><span>/</span><span>Карточка организации</span></div>
              <form className="search-field search-field--compact" onSubmit={handleGlobalSearch}>
                <Icon name="search" size={18} />
                <input value={globalSearch} onChange={(event) => setGlobalSearch(event.target.value)} placeholder="Поиск по вузам" aria-label="Поиск по вузам" />
              </form>
            </div>
            <ProfileHero
              university={selectedUniversity}
              onDownload={() => researchRef.current?.generate()}
            />
            <ResearchPanel key={selectedUniversity.id} ref={researchRef} university={selectedUniversity}
              showFacts={section === "contacts"} onShowFacts={() => setSection("contacts")}
              onConnected={(card) => setUniversities((current) => current.map((item) => item.id === selectedId
                ? { ...item, backendId: card.id, website: card.website, version: card.version } : item))} />
            <div className="profile-tabs" role="tablist" aria-label="Разделы карточки" onMouseLeave={() => setHoveredSection(null)}>
              {tabs.map((tab) => (
                <button
                  className={`profile-tab${section === tab.key ? " profile-tab--active" : ""}`}
                  type="button"
                  role="tab"
                  aria-selected={section === tab.key}
                  key={tab.key}
                  ref={(element) => { if (element) tabRefs.current[tab.key] = element; }}
                  onMouseEnter={() => setHoveredSection(tab.key)}
                  onFocus={() => setHoveredSection(tab.key)}
                  onBlur={() => setHoveredSection(null)}
                  onClick={() => { setSection(tab.key); setHoveredSection(tab.key); }}
                >
                  {tab.label}
                </button>
              ))}
              <span className="profile-tabs__indicator" aria-hidden="true" style={{ left: tabIndicator.left, width: tabIndicator.width }} />
            </div>
            <div className="profile-content page-enter" key={`${selectedUniversity.id}-${section}`}>
              {section === "about" && <AboutSection university={selectedUniversity} onAdd={() => setChatOpen(true)} />}
              {section === "projects" && <ProjectsSection university={selectedUniversity} />}
              {section === "events" && <EventsSection university={selectedUniversity} />}
            </div>
          </section>
        )}

        {view === "events" && (
          <section className="page-section events-page page-enter" key="events" aria-label="Мероприятия">
            <div className="page-heading-row">
              <div>
                <p className="eyebrow">ПОВЕСТКА ВУЗА</p>
                <h1>Мероприятия</h1>
                <p className="page-subtitle">События и анонсы для работы с выбранной организацией</p>
              </div>
              <button className="button button--soft" type="button" onClick={() => setChatOpen(true)}><Icon name="spark" size={17} /> Обсудить с агентом</button>
            </div>
            <div className="events-context"><span className="events-context__mark">{initialsFor(selectedUniversity.name)}</span><div><span className="muted-label">Выбранный вуз</span><strong>{selectedUniversity.name}</strong><span className="events-context__city"><Icon name="pin" size={14} /> {selectedUniversity.city}</span></div><button className="text-button" type="button" onClick={() => navigate("universities")}>Выбрать вуз <Icon name="arrow" size={16} /></button></div>
            <EventsList events={sampleEvents} demo />
          </section>
        )}

        {view === "reports" && (
          <section className="page-section reports-page page-enter" key="reports" aria-label="Сохранённые отчёты">
            <div className="page-heading-row page-heading-row--reports">
              <div>
                <p className="eyebrow">МАТЕРИАЛЫ К ВСТРЕЧАМ</p>
                <h1>Тут хранятся все сохранённые отчёты</h1>
                <p className="page-subtitle">Ищите, фильтруйте и открывайте отчёты в нужном формате</p>
              </div>
              <span className="reports-count">{filteredReports.length}<span>файла</span></span>
            </div>
            <div className="reports-toolbar">
              <label className="search-field reports-search">
                <Icon name="search" size={19} />
                <input value={reportSearch} onChange={(event) => setReportSearch(event.target.value)} placeholder="Поиск по отчётам" aria-label="Поиск по отчётам" />
              </label>
              <div className="filter-wrap">
                <button className={`button button--filter${reportFilterOpen ? " button--filter-active" : ""}`} type="button" onClick={() => setReportFilterOpen((open) => !open)} aria-expanded={reportFilterOpen}>
                  <Icon name="filter" size={18} /> {reportFilter === "Все форматы" ? "Фильтр" : reportFilter} <Icon name="chevron" size={15} />
                </button>
                {reportFilterOpen && (
                  <div className="filter-menu" role="menu">
                    {["Все форматы", "PPTX", "DOCX", "XLSX"].map((format) => (
                      <button type="button" role="menuitemradio" aria-checked={reportFilter === format} key={format} onClick={() => { setReportFilter(format); setReportFilterOpen(false); }}>
                        <span>{format}</span>{reportFilter === format && <Icon name="check" size={15} />}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <div className="report-table-wrap">
              <div className="report-table-head"><span>Название отчёта</span><span>Дата сохранения</span><span>Действия</span></div>
              <div className="report-list">
                {filteredReports.map((report, index) => (
                  <article className="report-row" key={report.id} style={{ animationDelay: `${index * 45}ms` }}>
                    <div className={`report-format report-format--${report.format.toLowerCase()}`}>{report.format}</div>
                    <div className="report-row__name"><strong>{report.name}</strong><span>{report.stale ? "Факты изменились — сформируйте новую версию" : "Черновик по принятым фактам"}</span></div>
                    <time className="report-row__date">{new Date(report.created_at).toLocaleDateString("ru-RU")}</time>
                    {report.stale ? <span className="demo-label">Архивная версия</span> : <a className="button button--small-primary" href={report.brief_url} target="_blank" rel="noreferrer">Справка</a>}
                    {report.stale ? <span /> : <a className="icon-button icon-button--small" href={report.download_url} aria-label="Скачать PPTX"><Icon name="download" size={17} /></a>}
                    <span />
                  </article>
                ))}
                {filteredReports.length === 0 && (
                  <div className="reports-empty"><Icon name="file" size={24} /><strong>Отчётов не найдено</strong><span>Измените поисковый запрос или формат.</span></div>
                )}
              </div>
            </div>
            <p className="reports-disclaimer">Файлы сохраняются после фактической сборки презентации. Перед встречей проверьте итоговый текст.</p>
          </section>
        )}
      </main>

      <AgentSphere onClick={() => setChatOpen(true)} hidden={chatOpen} />
      <AgentChat open={chatOpen} universityName={selectedUniversity.name} onClose={onChatClose}
        onAction={async (action) => {
          if (!researchRef.current) return "Сначала откройте карточку учреждения из списка вузов.";
          return action === "refresh" ? researchRef.current.refresh() : researchRef.current.generate();
        }} />

      {createOpen && (
        <Modal title="Новая карточка вуза" onClose={() => setCreateOpen(false)}>
          <p className="modal-copy">Добавьте название, регион и официальный сайт. Сведения о руководстве появятся после сбора и проверки.</p>
          <form className="modal-form" onSubmit={handleCreateUniversity}>
            <label>Название вуза<input autoFocus required value={createName} onChange={(event) => setCreateName(event.target.value)} placeholder="Например, университет или колледж" /></label>
            <label>Город или регион<input value={createCity} onChange={(event) => setCreateCity(event.target.value)} placeholder="Например, Казань" /></label>
            <label>Официальный сайт<input required type="url" value={createWebsite} onChange={(event) => setCreateWebsite(event.target.value)} placeholder="https://university.ru" /></label>
            <div className="modal-actions"><button className="button button--soft" type="button" onClick={() => setCreateOpen(false)}>Отмена</button><button className="button button--primary" type="submit" disabled={creating}><Icon name="plus" size={16} /> {creating ? "Сохранение…" : "Создать черновик"}</button></div>
          </form>
        </Modal>
      )}

      {contactOpen && (
        <Modal title="Добавить контакт" onClose={() => setContactOpen(false)}>
          <p className="modal-copy">Контакт сохранится в локальном черновике карточки «{selectedUniversity.name}».</p>
          <form className="modal-form" onSubmit={handleCreateContact}>
            <label>Имя и фамилия<input autoFocus required value={contactDraft.name} onChange={(event) => setContactDraft((current) => ({ ...current, name: event.target.value }))} placeholder="Имя контакта" /></label>
            <label>Должность<input value={contactDraft.role} onChange={(event) => setContactDraft((current) => ({ ...current, role: event.target.value }))} placeholder="Роль или подразделение" /></label>
            <div className="modal-form__columns">
              <label>Телефон<input value={contactDraft.phone} onChange={(event) => setContactDraft((current) => ({ ...current, phone: event.target.value }))} placeholder="+7 ..." /></label>
              <label>Электронная почта<input type="email" value={contactDraft.email} onChange={(event) => setContactDraft((current) => ({ ...current, email: event.target.value }))} placeholder="name@university.ru" /></label>
            </div>
            <div className="modal-actions"><button className="button button--soft" type="button" onClick={() => setContactOpen(false)}>Отмена</button><button className="button button--primary" type="submit">Сохранить контакт</button></div>
          </form>
        </Modal>
      )}

      {toast && <div className="toast" role="status"><span className="toast__check"><Icon name="check" size={15} /></span>{toast}</div>}
    </div>
  );
}

function ProfileHero({ university, onDownload }: { university: University; onDownload: () => void }) {
  return (
    <section className="profile-hero">
      <div className="profile-hero__decor profile-hero__decor--one" />
      <div className="profile-hero__decor profile-hero__decor--two" />
      <div className="profile-hero__copy">
        <span className="profile-hero__overline">КАРТОЧКА ОБРАЗОВАТЕЛЬНОЙ ОРГАНИЗАЦИИ</span>
        <h1>{university.name}</h1>
        <p className="profile-hero__meta"><Icon name="pin" size={15} /> {university.city}<span className="meta-separator">·</span>Проверка сведений по первоисточникам</p>
        <button className="button button--primary profile-hero__download" type="button" onClick={onDownload}><Icon name="file" size={16} /> Сформировать отчёт</button>
      </div>
      <div className="profile-hero__metrics">
        <div className="hero-metric hero-metric--green"><span className="hero-metric__icon"><Icon name="spark" size={15} /></span><span>Точки соприкосновений</span><strong>—</strong></div>
        <div className="hero-metric hero-metric--blue"><span className="hero-metric__icon"><Icon name="calendar" size={15} /></span><span>Ближайшие мероприятия</span><strong>—</strong></div>
      </div>
    </section>
  );
}

function AboutSection({ university, onAdd }: { university: University; onAdd: () => void }) {
  return (
    <div className="about-grid">
      <article className="content-card about-history">
        <div className="content-card__heading"><span className="section-icon"><Icon name="university" size={18} /></span><div><h2>История вуза</h2><span>Профиль организации</span></div><span className="fact-status"><span className="fact-status__dot" /> Ожидает данных</span></div>
        <p>Карточка «{university.name}» подготовлена для сбора и проверки сведений из открытых источников. Подтверждённая история, профиль деятельности и основные показатели появятся после загрузки данных.</p>
        <button className="inline-action" type="button" onClick={onAdd}><Icon name="plus" size={15} /> Добавить сведения</button>
      </article>
      <InfoListCard title="Структурные подразделения" icon="overview" items={[]} empty="Сведения о подразделениях ещё не добавлены." />
      <InfoListCard title="Основные направления подготовки" icon="spark" items={studyAreas} demo />
    </div>
  );
}

function ContactsSection({ university, contacts, onAdd }: { university: University; contacts: Contact[]; onAdd: () => void }) {
  return (
    <section className="content-card contacts-card">
      <div className="content-card__heading"><span className="section-icon"><Icon name="phone" size={18} /></span><div><h2>Руководство и контакты</h2><span>{university.name}</span></div><button className="button button--outline button--small-action" type="button" onClick={onAdd}><Icon name="plus" size={15} /> Добавить контакт</button></div>
      {contacts.length ? (
        <div className="contact-list">
          {contacts.map((contact, index) => (
            <article className="contact-row" key={`${contact.name}-${index}`}>
              <span className="contact-row__avatar">{initialsFor(contact.name)}</span>
              <div className="contact-row__identity"><strong>{contact.name}</strong><span>{contact.role || "Должность не указана"}</span><span className="contact-row__source"><span className="fact-status__dot" /> Черновик · ожидает проверки</span></div>
              <div className="contact-row__methods">{contact.phone && <span><Icon name="phone" size={15} /> {contact.phone}</span>}{contact.email && <span><Icon name="mail" size={15} /> {contact.email}</span>}</div>
            </article>
          ))}
        </div>
      ) : (
        <div className="empty-inline"><span className="empty-inline__icon"><Icon name="phone" size={21} /></span><strong>Контакты пока не добавлены</strong><p>Добавьте руководителя или приёмную. Данные останутся черновиком до проверки источника.</p><button className="button button--primary" type="button" onClick={onAdd}><Icon name="plus" size={16} /> Добавить первый контакт</button></div>
      )}
    </section>
  );
}

function ProjectsSection({ university }: { university: University }) {
  return (
    <div className="projects-grid">
      <InfoListCard title={`Проекты, реализуемые в ${university.name}`} icon="spark" items={sampleProjects} demo />
      <InfoListCard title="Возможности для сотрудничества" icon="overview" items={[]} empty="Точки соприкосновения появятся после анализа проверенных данных." />
    </div>
  );
}

function EventsSection({ university }: { university: University }) {
  return (
    <section className="content-card profile-events-card">
      <div className="content-card__heading"><span className="section-icon"><Icon name="calendar" size={18} /></span><div><h2>Мероприятия</h2><span>{university.name}</span></div><span className="demo-label">Демо-данные</span></div>
      <EventsList events={sampleEvents} demo />
    </section>
  );
}

function InfoListCard({ title, icon, items, empty, demo = false }: { title: string; icon: IconName; items: string[]; empty?: string; demo?: boolean }) {
  return (
    <article className="content-card info-list-card">
      <div className="content-card__heading"><span className="section-icon"><Icon name={icon} size={18} /></span><div><h2>{title}</h2>{demo && <span>Примеры для демонстрации интерфейса</span>}</div></div>
      {items.length ? (
        <ul className="fact-list">{items.map((item) => <li key={item}><span className="fact-list__bullet" />{item}<span className="demo-mark">пример</span></li>)}</ul>
      ) : (
        <div className="empty-list"><span className="empty-list__line" /><p>{empty}</p><span className="empty-list__line empty-list__line--short" /></div>
      )}
    </article>
  );
}

function EventsList({ events, demo }: { events: typeof sampleEvents; demo?: boolean }) {
  return (
    <div className="events-list-card">
      <div className="events-list-card__heading"><h2>Список мероприятий</h2>{demo && <span className="demo-label">Примеры</span>}</div>
      <div className="events-list">
        {events.map((event, index) => (
          <article className="event-row" key={event.title} style={{ animationDelay: `${index * 45}ms` }}>
            <span className="event-row__date"><span>НОЯ</span><strong>{String(4 + index * 3).padStart(2, "0")}</strong></span>
            <div className="event-row__copy"><strong>{event.title}</strong><span>{event.description}</span></div>
            <span className="event-row__demo">пример</span>
          </article>
        ))}
      </div>
    </div>
  );
}

function Modal({ children, title, onClose }: { children: ReactNode; title: string; onClose: () => void }) {
  return (
    <div className="modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-heading">
        <header className="modal__header"><div><span className="modal__eyebrow">КАРТОЧКА ВУЗА</span><h2 id="modal-heading">{title}</h2></div><button className="icon-button" type="button" onClick={onClose} aria-label="Закрыть окно"><Icon name="close" size={19} /></button></header>
        {children}
      </section>
    </div>
  );
}
