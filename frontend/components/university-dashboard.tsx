"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ResearchPanel, type ResearchHandle } from "@/components/research-panel";
import { CardEditHistory } from "@/components/card-edit-history";
import { PptxPreview } from "@/components/pptx-preview";
import { api, post, errorText, downloadFile, type ApiCard, type CardIntake, type Fact, type PptxPreviewData, type ResearchRun, type SavedMaterial } from "@/lib/api";
import { AgentChat } from "@/components/agent-chat";
import { AgentSphere } from "@/components/agent-sphere";
import { Icon, type IconName } from "@/components/icon";
import { buildPersonAliases, personIdentityKey } from "@/lib/person-identity";
import {
  initialsFor,
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
  { key: "collection", label: "Сбор" },
  { key: "contacts", label: "Руководство и контакты" },
  { key: "projects", label: "Проекты" },
  { key: "events", label: "Мероприятия" },
  { key: "review", label: "Проверка" },
  { key: "history", label: "История" },
];

export default function UniversityDashboard() {
  const [view, setView] = useState<View>("universities");
  const [section, setSection] = useState<Section>("about");
  const [hoveredSection, setHoveredSection] = useState<Section | null>(null);
  const [tabIndicator, setTabIndicator] = useState({ left: 0, width: 0 });
  const tabRefs = useRef<Partial<Record<Section, HTMLButtonElement>>>({});
  const [universities, setUniversities] = useState<University[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [hasChosenUniversity, setHasChosenUniversity] = useState(false);
  const [cardsLoading, setCardsLoading] = useState(true);
  const [cardsError, setCardsError] = useState("");
  const [universitySearch, setUniversitySearch] = useState("");
  const [globalSearch, setGlobalSearch] = useState("");
  const [regionFilter, setRegionFilter] = useState("Все регионы");
  const [chatOpen, setChatOpen] = useState(false);
  const [chatMode, setChatMode] = useState<"general" | "card">("general");
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createCity, setCreateCity] = useState("");
  const [createWebsite, setCreateWebsite] = useState("");
  const [createWebsiteSuggestion, setCreateWebsiteSuggestion] = useState(false);
  const [creating, setCreating] = useState(false);
  const researchRef = useRef<ResearchHandle>(null);
  const [reviewOutlet, setReviewOutlet] = useState<HTMLDivElement | null>(null);
  const [runs, setRuns] = useState<Record<string, ResearchRun | null>>({});
  const [cardEditVersion, setCardEditVersion] = useState(0);
  const [reportSearch, setReportSearch] = useState("");
  const [reports, setReports] = useState<SavedMaterial[]>([]);
  const [reportsLoading, setReportsLoading] = useState(false);
  const [reportsError, setReportsError] = useState("");
  const [reportFilter, setReportFilter] = useState("Все форматы");
  const [reportFilterOpen, setReportFilterOpen] = useState(false);
  const [previewReport, setPreviewReport] = useState<SavedMaterial | null>(null);
  const [presentationPreview, setPresentationPreview] = useState<PptxPreviewData | null>(null);
  const [reportPreviewText, setReportPreviewText] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [cardDeleteTarget, setCardDeleteTarget] = useState<University | null>(null);
  const [deletingCardId, setDeletingCardId] = useState<string | null>(null);
  const [cardDeleteError, setCardDeleteError] = useState("");
  const [reportDeleteTarget, setReportDeleteTarget] = useState<string | null>(null);
  const [deletingReportId, setDeletingReportId] = useState<string | null>(null);
  const [reportDeleteError, setReportDeleteError] = useState("");
  const [toast, setToast] = useState("");

  const selectedUniversity = universities.find((university) => university.id === selectedId);
  const chatCard = hasChosenUniversity ? selectedUniversity : undefined;
  const selectedRun = selectedId ? runs[selectedId] ?? null : null;
  const regions = useMemo(() => [...new Set(universities.map((university) => university.city))], [universities]);
  const filteredUniversities = universities.filter((university) => {
    const matchesQuery = `${university.name} ${university.city}`.toLocaleLowerCase("ru-RU").includes(universitySearch.toLocaleLowerCase("ru-RU").trim());
    const matchesRegion = regionFilter === "Все регионы" || university.city === regionFilter;
    return matchesQuery && matchesRegion;
  });
  const filteredReports = reports.filter((report) => {
    if (/^xls(?:x|m|b)?$/i.test(report.format.trim())) return false;
    const matchesQuery = `${report.name} ${report.format}`.toLocaleLowerCase("ru-RU").includes(reportSearch.toLocaleLowerCase("ru-RU").trim());
    return matchesQuery && (reportFilter === "Все форматы" || report.format === reportFilter);
  });
  const indicatorSection = hoveredSection ?? section;

  useEffect(() => {
    if (view !== "reports") return;
    let active = true;
    setReportsLoading(true);
    setReportsError("");
    api<SavedMaterial[]>("/materials").then((items) => {
      if (active) setReports(items);
    }).catch((failure) => {
      if (active) setReportsError(errorText(failure));
    }).finally(() => {
      if (active) setReportsLoading(false);
    });
    return () => { active = false; };
  }, [view]);

  useEffect(() => {
    let active = true;
    api<ApiCard[]>("/cards").then((cards) => {
      if (!active) return;
      const persisted: University[] = cards.map((card) => ({ ...card, id: card.id, backendId: card.id,
        tone: "mint" }));
      setUniversities(persisted);
      setSelectedId((current) => persisted.some((item) => item.id === current) ? current : persisted[0]?.id ?? "");
      setCardsError("");
    }).catch((failure) => {
      if (active) setCardsError(errorText(failure));
    }).finally(() => { if (active) setCardsLoading(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const restoreView = () => {
      const params = new URLSearchParams(window.location.search);
      const savedView = params.get("vuzView");
      const nextView = navigation.some((item) => item.key === savedView) ? savedView as View : "universities";
      const universityId = params.get("university");
      setView(nextView);
      if (nextView === "overview") setSection("about");
      if (nextView === "events") setSection("events");
      if (universityId) {
        setSelectedId(universityId);
        setHasChosenUniversity(true);
      }
      setReportFilterOpen(false);
    };
    window.addEventListener("popstate", restoreView);
    restoreView();
    return () => window.removeEventListener("popstate", restoreView);
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

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST", cache: "no-store" }).catch(() => undefined);
    window.location.replace("/login");
  }

  const openPresentationPreview = async (report: SavedMaterial) => {
    setPreviewReport(report);
    setPresentationPreview(null);
    setReportPreviewText("");
    setPreviewError("");
    setPreviewLoading(true);
    try {
      if (report.format === "DOCX") {
        const response = await fetch(report.brief_url, { cache: "no-store" });
        if (!response.ok) throw new Error("preview_unavailable");
        setReportPreviewText(await response.text());
      } else {
        const response = await fetch(report.preview_url, { cache: "no-store" });
        if (!response.ok) throw new Error("preview_unavailable");
        const preview = await response.json() as PptxPreviewData;
        if (!preview.slides.length) throw new Error("preview_unavailable");
        setPresentationPreview(preview);
      }
    } catch {
      setPreviewError("Не удалось загрузить слайды презентации. Возможно, материал устарел.");
    } finally {
      setPreviewLoading(false);
    }
  };

  const downloadPresentation = async (report: SavedMaterial) => {
    try {
      await downloadFile(report.download_url, `${report.name}.${report.format.toLowerCase()}`);
    } catch (failure) {
      notify(errorText(failure));
    }
  };

  const deleteReport = async (report: SavedMaterial) => {
    setDeletingReportId(report.id);
    setReportDeleteError("");
    try {
      await api<{ deleted: boolean }>(`/materials/${encodeURIComponent(report.id)}/report`, { method: "DELETE" });
      setReports((current) => current.filter((item) => item.id !== report.id));
      setReportDeleteTarget(null);
      notify("Отчет удален.");
    } catch (failure) {
      setReportDeleteError(errorText(failure));
    } finally {
      setDeletingReportId(null);
    }
  };

  const confirmDeleteCard = async () => {
    const target = cardDeleteTarget;
    if (!target?.backendId || deletingCardId) return;
    const backendId = target.backendId;
    setDeletingCardId(backendId);
    setCardDeleteError("");
    try {
      await api<void>(`/cards/${encodeURIComponent(backendId)}`, { method: "DELETE" });
      const remaining = universities.filter((item) => item.backendId !== backendId);
      setUniversities(remaining);
      setRuns((current) => {
        const next = { ...current };
        delete next[target.id];
        return next;
      });
      setSelectedId(remaining[0]?.id ?? "");
      setHasChosenUniversity(remaining.length > 0);
      setSection("about");
      setChatOpen(false);
      setChatMode("general");
      setCardDeleteTarget(null);
      changeView("universities", remaining[0]?.id ?? "");
      notify(`Карточка «${target.name}» удалена.`);
    } catch (failure) {
      setCardDeleteError(errorText(failure));
    } finally {
      setDeletingCardId(null);
    }
  };

  const changeView = (nextView: View, nextUniversityId = selectedId) => {
    if (nextView === view) return;
    const params = new URLSearchParams(window.location.search);
    params.set("vuzView", nextView);
    if (nextUniversityId) params.set("university", nextUniversityId);
    else params.delete("university");
    const nextUrl = `${window.location.pathname}?${params.toString()}${window.location.hash}`;
    window.history.pushState(null, "", nextUrl);
    setView(nextView);
    if (nextView === "overview") setSection("about");
    if (nextView === "events") setSection("events");
    setReportFilterOpen(false);
  };

  const navigate = (nextView: View) => {
    if ((nextView === "overview" || nextView === "events") && (!hasChosenUniversity || !selectedUniversity)) return;
    changeView(nextView);
  };

  const openUniversity = (university: University) => {
    setSelectedId(university.id);
    setHasChosenUniversity(true);
    changeView("overview", university.id);
  };

  const handleGlobalSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setUniversitySearch(globalSearch);
    setRegionFilter("Все регионы");
    changeView("universities");
  };

  const openCreate = (suggestedName = "") => {
    setCreateName(suggestedName);
    setCreateCity("");
    setCreateWebsite("");
    setCreateWebsiteSuggestion(false);
    setCreateOpen(true);
  };

  const handleCreateUniversity = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!createName.trim()) return;
    setCreating(true);
    try {
    const intake = await post<CardIntake>("/cards/intake", {
      name: createName.trim(), city: createCity.trim(), website: createWebsite.trim() || null,
      request_key: crypto.randomUUID(),
    });
    const card = intake.card;
    const university: University = { ...card, backendId: card.id, tone: "mint" };
    setSection("collection");
    setUniversities((current) => [university, ...current]);
    setSelectedId(university.id);
    setHasChosenUniversity(true);
    setCreateOpen(false);
    setUniversitySearch("");
    setGlobalSearch("");
    setRegionFilter("Все регионы");
    changeView("overview", university.id);
    notify(intake.job.status === "queued" || intake.job.status === "running"
      ? "Карточка создана. Сбор начался; результат появится в карточке."
      : intake.job.message);
    } catch (failure) {
      if (failure instanceof Error && ["official_site_not_found", "official_site_discovery_failed"].includes(failure.message)) {
        setCreateWebsiteSuggestion(true);
      } else notify(errorText(failure));
    }
    finally { setCreating(false); }
  };

  const onChatClose = () => setChatOpen(false);
  const openChat = (mode: "general" | "card") => {
    setChatMode(mode);
    setChatOpen(true);
  };

  const openChatCard = (card: ApiCard) => {
    const university: University = { ...card, backendId: card.id, tone: "mint" };
    setUniversities((current) => [university, ...current.filter((item) => item.backendId !== card.id)]);
    openUniversity(university);
    setCardEditVersion((version) => version + 1);
  };

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
                disabled={(item.key === "overview" || item.key === "events") && (!hasChosenUniversity || !selectedUniversity)}
                onClick={() => navigate(item.key)}
                aria-current={view === item.key ? "page" : undefined}
                aria-disabled={(item.key === "overview" || item.key === "events") && (!hasChosenUniversity || !selectedUniversity)}
              >
                <span className="nav-link__icon"><Icon name={item.icon} size={18} /></span>
                <span>{item.label}</span>
                {view === item.key && <span className="nav-link__check"><Icon name="check" size={12} /></span>}
              </button>
            ))}
          </nav>
        </div>
        <button className="user-card" type="button" onClick={() => { void logout(); }} aria-label="Выйти из системы" title="Выйти из системы">
          <span className="user-card__avatar">ИИ</span>
          <span className="user-card__name">Выйти</span>
          <Icon className="user-card__dots" name="arrow" size={18} />
        </button>
      </aside>

      <main className="main-area" id="top">
        <div className="mobile-topbar">
          <button className="mobile-agent-button" type="button" onClick={() => { void logout(); }}>
            Выйти
          </button>
          <button className="mobile-agent-button" type="button" onClick={() => openChat("general")}>
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

            {cardsLoading ? (
              <div className="empty-state empty-state--search">
                <span className="empty-state__icon"><Icon name="university" size={24} /></span>
                <h2>Загружаем карточки</h2>
                <p>Подключаемся к базе сохранённых организаций.</p>
              </div>
            ) : filteredUniversities.length > 0 ? (
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
                <h2>{cardsError ? "Не удалось загрузить карточки" : universities.length ? "Такого вуза пока нет в базе" : "Карточек вузов пока нет"}</h2>
                <p>{cardsError || "Создайте карточку — она сохранится в базе и появится в списке после перезагрузки."}</p>
                <button className="button button--primary" type="button" onClick={() => openCreate(universitySearch)}>
                  <Icon name="plus" size={17} /> Создать карточку
                </button>
              </div>
            )}
            <div className="page-caption"><span className="caption-dot" /> Карточки организаций · {universities.length}</div>
          </section>
        )}

        {view === "overview" && selectedUniversity && (
          <section className="page-section profile-page page-enter" key={`profile-${selectedUniversity.id}`} aria-label={`Карточка: ${selectedUniversity.name}`}>
            <div className="profile-topbar">
              <div className="breadcrumbs"><button type="button" onClick={() => navigate("universities")}>ВУЗы</button><span>/</span><span>Карточка организации</span></div>
              <div className="profile-topbar__actions">
                <form className="search-field search-field--compact" onSubmit={handleGlobalSearch}>
                  <Icon name="search" size={18} />
                  <input value={globalSearch} onChange={(event) => setGlobalSearch(event.target.value)} placeholder="Поиск по вузам" aria-label="Поиск по вузам" />
                </form>
                {selectedUniversity.backendId && <button className="button button--danger profile-delete-button" type="button"
                  onClick={() => { setCardDeleteError(""); setCardDeleteTarget(selectedUniversity); }}>
                  <Icon name="trash" size={15} /> Удалить карточку
                </button>}
              </div>
            </div>
            <ProfileHero university={selectedUniversity} />
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
            <div className="collection-panel-slot" hidden={section !== "collection"} aria-hidden={section !== "collection"}>
              <ResearchPanel key={selectedUniversity.id + "-" + cardEditVersion} ref={researchRef} university={selectedUniversity}
                showFacts={section === "review"} reviewOutlet={reviewOutlet} onShowFacts={() => setSection("review")}
                onRunChange={(run) => setRuns((current) => ({ ...current, [selectedUniversity.id]: run }))}
                onConnected={(card) => setUniversities((current) => current.map((item) => item.id === selectedId
                  ? { ...item, backendId: card.id, website: card.website, version: card.version } : item))} />
            </div>
            {section !== "collection" && <div className="profile-content page-enter" key={`${selectedUniversity.id}-${section}`}>
              {section === "review" ? <div className="review-outlet" ref={setReviewOutlet} /> : <>
              {section === "about" && <AboutSection university={selectedUniversity} run={selectedRun} onAdd={() => openChat("card")} />}
              {section === "contacts" && <ContactsSection university={selectedUniversity} run={selectedRun} />}
              {section === "projects" && <ProjectsSection university={selectedUniversity} run={selectedRun} />}
              {section === "events" && <EventsSection university={selectedUniversity} run={selectedRun} />}
              {section === "history" && selectedUniversity.backendId && <CardEditHistory cardId={selectedUniversity.backendId} />}
              </>}
            </div>}
          </section>
        )}

        {view === "events" && selectedUniversity && (
          <section className="page-section events-page page-enter" key="events" aria-label="Мероприятия">
            <div className="page-heading-row">
              <div>
                <p className="eyebrow">ПОВЕСТКА ВУЗА</p>
                <h1>Мероприятия</h1>
                <p className="page-subtitle">События и анонсы для работы с выбранной организацией</p>
              </div>
              <button className="button button--soft" type="button" onClick={() => openChat("card")}><Icon name="spark" size={17} /> Обсудить с агентом</button>
            </div>
            <div className="events-context"><span className="events-context__mark">{initialsFor(selectedUniversity.name)}</span><div><span className="muted-label">Выбранный вуз</span><strong>{selectedUniversity.name}</strong><span className="events-context__city"><Icon name="pin" size={14} /> {selectedUniversity.city}</span></div><button className="text-button" type="button" onClick={() => navigate("universities")}>Выбрать вуз <Icon name="arrow" size={16} /></button></div>
            <ContextEntityList kind="event" run={selectedRun} empty="Проверенных мероприятий в карточке пока нет." />
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
                    {["Все форматы", "PPTX", "DOCX"].map((format) => (
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
                {reportsLoading ? (
                  <div className="reports-empty"><Icon name="file" size={24} /><strong>Загружаем отчёты</strong><span>Получаем сохранённые материалы из базы.</span></div>
                ) : reportsError ? (
                  <div className="reports-empty"><Icon name="file" size={24} /><strong>Не удалось загрузить отчёты</strong><span>{reportsError}</span></div>
                ) : <>
                  {filteredReports.map((report, index) => (
                    <article className={`report-row${reportDeleteTarget === report.id ? " report-row--delete-open" : ""}`} key={report.id} style={{ animationDelay: `${index * 45}ms` }}>
                      <div className={`report-format report-format--${report.format.toLowerCase()}`}>{report.format}</div>
                      <div className="report-row__name"><strong>{report.name}</strong><span>{report.stale ? "Архивная версия сбора" : "Черновик по принятым фактам"}</span></div>
                      <time className="report-row__date">{new Date(report.created_at).toLocaleDateString("ru-RU")}</time>
                      <button className="button button--small-primary report-preview-button" type="button" onClick={() => { void openPresentationPreview(report); }} aria-label={`Предпросмотр отчёта: ${report.name}`}><Icon name="search" size={15} /><span>Предпросмотр</span></button>
                      <button className="icon-button icon-button--small report-download-button" type="button" onClick={() => { void downloadPresentation(report); }} aria-label={`Скачать ${report.format}`}><Icon name="download" size={17} /></button>
                      <div className="report-delete-control">
                        <button className="icon-button icon-button--small report-delete-button" type="button"
                          onClick={() => {
                            setReportDeleteError("");
                            setReportDeleteTarget((target) => target === report.id ? null : report.id);
                          }}
                          aria-label={`Удалить отчет: ${report.name}`} aria-expanded={reportDeleteTarget === report.id}
                          aria-controls={`delete-report-${report.id}`}>
                          <Icon name="trash" size={16} />
                        </button>
                        {reportDeleteTarget === report.id && <div className="report-delete-popover" id={`delete-report-${report.id}`} role="dialog" aria-label="Подтверждение удаления отчета">
                          <strong>Удалить отчет?</strong>
                          <span>{report.name}</span>
                          {reportDeleteError && <span className="report-delete-popover__error" role="alert">{reportDeleteError}</span>}
                          <div className="report-delete-popover__actions">
                            <button className="button button--small-primary" type="button" disabled={deletingReportId === report.id} onClick={() => { void deleteReport(report); }}>{deletingReportId === report.id ? "Удаляем…" : "Удалить"}</button>
                            <button className="button button--soft" type="button" disabled={deletingReportId === report.id} onClick={() => setReportDeleteTarget(null)}>Отмена</button>
                          </div>
                        </div>}
                      </div>
                    </article>
                  ))}
                  {filteredReports.length === 0 && (
                    <div className="reports-empty"><Icon name="file" size={24} /><strong>Отчётов не найдено</strong><span>Измените поисковый запрос или формат.</span></div>
                  )}
                </>}
              </div>
            </div>
            <p className="reports-disclaimer">Сохраняются последние два файла по каждому вузу. Старые версии доступны как архивные.</p>
          </section>
        )}
      </main>

      {previewReport && (
        <div className="report-preview-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPreviewReport(null); }}>
          <section className="report-preview" role="dialog" aria-modal="true" aria-labelledby="saved-presentation-preview-title">
            <header className="report-preview__header">
              <div><span className="eyebrow">ПРЕДПРОСМОТР ПРЕЗЕНТАЦИИ</span><h2 id="saved-presentation-preview-title">{previewReport.name}</h2></div>
              <button className="icon-button" type="button" onClick={() => setPreviewReport(null)} aria-label="Закрыть предпросмотр"><Icon name="close" size={19} /></button>
            </header>
            <div className="report-preview__content">
              {previewLoading && <div className="reports-empty"><Icon name="file" size={24} /><strong>Загружаем презентацию</strong><span>Подготавливаем предварительный просмотр слайдов.</span></div>}
              {previewError && <div className="reports-empty" role="alert"><Icon name="file" size={24} /><strong>Предпросмотр недоступен</strong><span>{previewError}</span></div>}
              {!previewLoading && !previewError && presentationPreview && <PptxPreview preview={presentationPreview} />}
              {!previewLoading && !previewError && reportPreviewText && reportPreviewText.split("\n").map((line, index) => {
                if (line.startsWith("# ")) return <h3 key={index}>{line.slice(2)}</h3>;
                if (line.startsWith("## ")) return <h4 key={index}>{line.slice(3)}</h4>;
                return line.trim() ? <p key={index}>{line}</p> : <br key={index} />;
              })}
            </div>
            <footer className="report-preview__actions">
              <span>{previewLoading ? "Загрузка отчёта…" : previewReport.format === "DOCX" ? "Предпросмотр справки · файл DOCX" : presentationPreview ? `${presentationPreview.slides.length} слайдов · предпросмотр файла PPTX` : ""}</span>
              <div className="report-preview__button-group">
                <button className="button button--soft" type="button" disabled><Icon name="check" size={16} />Уже в отчётах</button>
                <button className="button button--primary" type="button" disabled={!presentationPreview && !reportPreviewText} onClick={() => { void downloadPresentation(previewReport); }}><Icon name="download" size={16} />Скачать на компьютер</button>
              </div>
            </footer>
          </section>
        </div>
      )}

      <AgentSphere onClick={() => openChat("general")} hidden={chatOpen} />
      <AgentChat open={chatOpen}
        cardId={chatCard?.backendId} mode={chatMode} onModeChange={setChatMode}
        universityName={chatCard?.name ?? "Общий чат"} onClose={onChatClose}
        onOpenCard={(card) => { openChatCard(card); setChatMode("card"); }}
        onEditApplied={() => setCardEditVersion((version) => version + 1)}
        onCollection={(card) => {
          setSection("collection");
          openChatCard(card);
          setChatMode("card");
        }}
        onAction={async (action) => {
          if (!researchRef.current) return { message: "Сначала откройте карточку учреждения из списка вузов." };
          setSection("collection");
          return action === "refresh" ? researchRef.current.refresh() : researchRef.current.generate();
        }}
        onShowRecheck={() => {
          setChatOpen(false);
          setSection("collection");
          requestAnimationFrame(() => researchRef.current?.showRecheck());
        }} />

      {createOpen && (
        <Modal title="Новая карточка вуза" onClose={() => setCreateOpen(false)}>
          <p className="modal-copy">Название обязательно. Сайт можно не указывать: мы попробуем найти его автоматически. Если адрес не удастся определить, предложим ввести ссылку вручную.</p>
          <form className="modal-form" onSubmit={handleCreateUniversity}>
            <label>Название вуза<input autoFocus required value={createName} onChange={(event) => setCreateName(event.target.value)} placeholder="Например, университет или колледж" /></label>
            <label>Город или регион<input value={createCity} onChange={(event) => setCreateCity(event.target.value)} placeholder="Например, Казань" /></label>
            <label>Официальный сайт (необязательно)<input type="url" value={createWebsite} onChange={(event) => {
              setCreateWebsite(event.target.value);
              if (event.target.value.trim()) setCreateWebsiteSuggestion(false);
            }} placeholder="https://university.ru" /></label>
            {createWebsiteSuggestion && <p className="modal-form__hint" role="status">Автоматически найти официальный сайт не удалось. Если знаете ссылку, добавьте её сюда — мы попробуем найти больше информации.</p>}
            <div className="modal-actions"><button className="button button--soft" type="button" onClick={() => setCreateOpen(false)}>Отмена</button><button className="button button--primary" type="submit" disabled={creating}><Icon name="plus" size={16} /> {creating ? createWebsite.trim() ? "Запускаем сбор…" : "Ищем сайт…" : "Создать и собрать"}</button></div>
          </form>
        </Modal>
      )}

      {cardDeleteTarget && <Modal title="Удалить карточку?" dismissible={!deletingCardId}
        onClose={() => { if (!deletingCardId) setCardDeleteTarget(null); }}>
        <p className="modal-copy">Карточка «{cardDeleteTarget.name}» и связанные с ней сведения будут удалены без возможности восстановления.</p>
        {cardDeleteError && <p className="research-error" role="alert">Не удалось удалить карточку. {cardDeleteError}</p>}
        <div className="modal-actions">
          <button className="button button--soft" type="button" disabled={Boolean(deletingCardId)}
            onClick={() => setCardDeleteTarget(null)}>Отмена</button>
          <button className="button button--danger" type="button" disabled={Boolean(deletingCardId)}
            onClick={() => { void confirmDeleteCard(); }}>
            {deletingCardId ? "Удаляем…" : "Удалить безвозвратно"}
          </button>
        </div>
      </Modal>}

      {toast && <div className="toast" role="status"><span className="toast__check"><Icon name="check" size={15} /></span>{toast}</div>}
    </div>
  );
}

function ProfileHero({ university }: { university: University }) {
  return (
    <section className="profile-hero">
      <div className="profile-hero__decor profile-hero__decor--one" />
      <div className="profile-hero__decor profile-hero__decor--two" />
      <div className="profile-hero__copy">
        <span className="profile-hero__overline">КАРТОЧКА ОБРАЗОВАТЕЛЬНОЙ ОРГАНИЗАЦИИ</span>
        <h1>{university.name}</h1>
        <p className="profile-hero__meta"><Icon name="pin" size={15} /> {university.city}</p>
      </div>
      <div className="profile-hero__metrics">
        <div className="hero-metric hero-metric--green"><span className="hero-metric__icon"><Icon name="spark" size={15} /></span><span>Точки соприкосновений</span><strong>—</strong></div>
        <div className="hero-metric hero-metric--blue"><span className="hero-metric__icon"><Icon name="calendar" size={15} /></span><span>Ближайшие мероприятия</span><strong>—</strong></div>
      </div>
    </section>
  );
}

const factLabels: Record<string, string> = {
  "person.name": "ФИО", "appointment.position": "Должность", "appointment.status": "Статус назначения",
  "appointment.department": "Подразделение", "appointment.responsibilities": "Зона ответственности",
  "contact.phone": "Телефон", "contact.email": "Почта", "person.career": "Биография",
  "person.education": "Образование", "person.degree": "Учёная степень", "institution.profile": "Профиль",
  "project.program": "Программа", "project.role": "Роль", "project.status": "Статус",
  "project.period": "Период", "project.description": "Описание", "event.type": "Тип",
  "event.date": "Дата", "event.status": "Статус", "event.description": "Описание",
};

function isVerified(fact: Fact) {
  return fact.verification_status === "verified" && fact.validation_status === "passed" && !fact.issues.length;
}

function verifiedFacts(run: ResearchRun | null) {
  if (!run) return [];
  const retired = new Set(run.retired_fact_ids ?? []);
  const active = new Map([...(run.baseline_facts ?? []), ...run.facts].map((fact) => [fact.fact_id, fact]));
  const facts = [...active.values()].filter((fact) => isVerified(fact) && !retired.has(fact.fact_id));
  const aliases = buildPersonAliases(facts.flatMap((fact) => {
    const name = fact.subject.person_name ?? (fact.contact_owner?.kind === "person" ? fact.contact_owner.label : null);
    return name ? [{ name, department: fact.subject.department }] : [];
  }));
  const seen = new Set<string>();
  return facts.filter((fact) => {
    const name = fact.subject.person_name ?? (fact.contact_owner?.kind === "person" ? fact.contact_owner.label : null);
    const owner = name ? `person:${personIdentityKey(name, fact.subject.department, aliases)}`
      : fact.subject.entity_id ? `entity:${fact.subject.entity_kind ?? "entity"}:${fact.subject.entity_id}`
        : fact.contact_owner ? `contact:${fact.contact_owner.kind}:${fact.contact_owner.label.normalize("NFKC").toLocaleLowerCase("ru-RU").replace(/ё/g, "е").trim()}`
          : `institution:${fact.subject.entity_name ?? ""}`;
    const key = `${owner}\u0000${fact.field}\u0000${fact.value.normalize("NFKC").toLocaleLowerCase("ru-RU").replace(/ё/g, "е").replace(/\s+/g, " ").trim()}\u0000${JSON.stringify(fact.metric_context ?? null)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function groupVerifiedEntities(run: ResearchRun | null, kind: "project" | "event") {
  const groups = new Map<string, { name: string; facts: Fact[] }>();
  for (const fact of verifiedFacts(run)) {
    if (fact.subject.entity_kind !== kind || !fact.subject.entity_id || !fact.subject.entity_name) continue;
    const entity = groups.get(fact.subject.entity_id) ?? { name: fact.subject.entity_name, facts: [] };
    entity.facts.push(fact);
    groups.set(fact.subject.entity_id, entity);
  }
  return [...groups.entries()].map(([id, entity]) => ({ id, ...entity }));
}

function AboutSection({ university, run, onAdd }: { university: University; run: ResearchRun | null; onAdd: () => void }) {
  const profile = verifiedFacts(run).find((fact) => fact.field === "institution.profile");
  const departments = [...new Set(verifiedFacts(run)
    .filter((fact) => fact.field === "appointment.department")
    .map((fact) => fact.value))];
  return (
    <div className="about-grid">
      <article className="content-card about-history">
        <div className="content-card__heading"><span className="section-icon"><Icon name="university" size={18} /></span><div><h2>Профиль организации</h2><span>{university.name}</span></div><span className="fact-status"><span className="fact-status__dot" /> {profile ? "Проверено" : "Ожидает данных"}</span></div>
        <p>{profile?.value ?? "Подтверждённые сведения появятся после сбора и проверки источников."}</p>
        {profile?.evidence[0] && <a className="text-button" href={profile.evidence[0].url} target="_blank" rel="noreferrer">Источник <Icon name="external" size={14} /></a>}
        <button className="inline-action" type="button" onClick={onAdd}><Icon name="spark" size={15} /> Обсудить с агентом</button>
      </article>
      <InfoListCard title="Структурные подразделения" icon="overview" items={departments} empty="Проверенных подразделений пока нет." />
      <InfoListCard title="Официальный сайт" icon="external" items={university.website ? [university.website] : []} empty="Сайт не указан в карточке." />
    </div>
  );
}

function ContactsSection({ university, run }: { university: University; run: ResearchRun | null }) {
  const people = new Map<string, { name: string; facts: Fact[] }>();
  const facts = verifiedFacts(run).filter((fact) => !fact.subject.entity_kind);
  const aliases = buildPersonAliases(facts.flatMap((fact) => {
    const name = fact.subject.person_name ?? (fact.contact_owner?.kind === "person" ? fact.contact_owner.label : null);
    return name ? [{ name, department: fact.subject.department }] : [];
  }));
  for (const fact of facts) {
    if (fact.subject.entity_kind) continue;
    const name = fact.subject.person_name ?? (fact.contact_owner?.kind === "person" ? fact.contact_owner.label : null) ?? fact.contact_owner?.label ?? "Контакт организации";
    const key = fact.subject.person_name || fact.contact_owner?.kind === "person"
      ? `person:${personIdentityKey(name, fact.subject.department, aliases)}`
      : fact.contact_owner
        ? `contact:${fact.contact_owner.kind}:${fact.contact_owner.label.normalize("NFKC").toLocaleLowerCase("ru-RU").replace(/ё/g, "е").trim()}`
        : "contact:organization";
    const group = people.get(key) ?? { name, facts: [] };
    group.facts.push(fact);
    if (name.split(/\s+/).length > group.name.split(/\s+/).length) group.name = name;
    people.set(key, group);
  }
  const entries = [...people.entries()].map(([id, person]) => ({ id, ...person }));
  const factPriority: Record<string, number> = { "appointment.position": 0, "contact.email": 1, "contact.phone": 2 };
  return (
    <section className="content-card contacts-card">
      <div className="content-card__heading"><span className="section-icon"><Icon name="phone" size={18} /></span><div><h2>Проверенное руководство и контакты</h2><span>{university.name} · данные из сохранённого исследования</span></div></div>
      {entries.length ? <div className="contact-list">
        {entries.map((person) => <article className="contact-row" key={person.id}>
          <span className="contact-row__avatar">{initialsFor(person.name)}</span>
          <div className="contact-row__identity"><strong>{person.name}</strong>{[...person.facts].sort((a, b) => (factPriority[a.field] ?? 3) - (factPriority[b.field] ?? 3)).map((fact) => <span key={fact.fact_id}>{factLabels[fact.field] ?? fact.field}: {fact.value}</span>)}</div>
          <div className="contact-row__methods">{person.facts.filter((fact) => fact.evidence[0]).slice(0, 2).map((fact) => <a key={fact.fact_id} href={fact.evidence[0].url} target="_blank" rel="noreferrer">Источник <Icon name="external" size={13} /></a>)}</div>
        </article>)}
      </div> : <div className="empty-inline"><span className="empty-inline__icon"><Icon name="phone" size={21} /></span><strong>Проверенных контактов пока нет</strong><p>Запустите сбор в панели выше и проверьте найденные факты и источники.</p></div>}
    </section>
  );
}

function ContextEntityList({ kind, run, empty, title }: { kind: "project" | "event"; run: ResearchRun | null; empty: string; title?: string }) {
  const entities = groupVerifiedEntities(run, kind);
  const label = kind === "project" ? "Проекты" : "Мероприятия";
  return (
    <section className="content-card context-entities">
      <div className="content-card__heading"><span className="section-icon"><Icon name={kind === "project" ? "spark" : "calendar"} size={18} /></span><div><h2>{title ?? label}</h2><span>Подтверждённые данные из сохранённого исследования</span></div></div>
      {entities.length ? <div className="context-entity-list">
        {entities.map((entity) => <article className="context-entity" key={entity.id}>
          <h3>{entity.name}</h3>
          {entity.facts.filter((fact) => !fact.field.endsWith(".name")).map((fact) => <p className="context-entity__fact" key={fact.fact_id}>
            <strong>{factLabels[fact.field] ?? fact.field}:</strong> {fact.value}
            {fact.evidence[0] && <a href={fact.evidence[0].url} target="_blank" rel="noreferrer">Источник <Icon name="external" size={12} /></a>}
          </p>)}
        </article>)}
      </div> : <div className="empty-list"><span className="empty-list__line" /><p>{empty}</p><span className="empty-list__line empty-list__line--short" /></div>}
    </section>
  );
}

function ProjectsSection({ university, run }: { university: University; run: ResearchRun | null }) {
  return (
    <div className="projects-grid">
      <ContextEntityList kind="project" run={run} title={`Проекты в ${university.name}`} empty="Проверенных проектов пока нет." />
      <InfoListCard title="Возможности для сотрудничества" icon="overview" items={[]} empty="Подтверждённые точки соприкосновения появятся после анализа данных." />
    </div>
  );
}

function EventsSection({ university, run }: { university: University; run: ResearchRun | null }) {
  return <ContextEntityList kind="event" run={run} title={`Мероприятия · ${university.name}`} empty="Проверенных мероприятий пока нет." />;
}

function InfoListCard({ title, icon, items, empty }: { title: string; icon: IconName; items: string[]; empty?: string }) {
  return (
    <article className="content-card info-list-card">
      <div className="content-card__heading"><span className="section-icon"><Icon name={icon} size={18} /></span><div><h2>{title}</h2></div></div>
      {items.length ? (
        <ul className="fact-list">{items.map((item) => <li key={item}><span className="fact-list__bullet" />{item}</li>)}</ul>
      ) : (
        <div className="empty-list"><span className="empty-list__line" /><p>{empty}</p><span className="empty-list__line empty-list__line--short" /></div>
      )}
    </article>
  );
}

function Modal({ children, title, onClose, dismissible = true }: { children: ReactNode; title: string; onClose: () => void; dismissible?: boolean }) {
  return (
    <div className="modal-layer" role="presentation" onMouseDown={(event) => { if (dismissible && event.target === event.currentTarget) onClose(); }}>
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-heading">
        <header className="modal__header"><div><span className="modal__eyebrow">КАРТОЧКА ВУЗА</span><h2 id="modal-heading">{title}</h2></div><button className="icon-button" type="button" disabled={!dismissible} onClick={onClose} aria-label="Закрыть окно"><Icon name="close" size={19} /></button></header>
        {children}
      </section>
    </div>
  );
}
