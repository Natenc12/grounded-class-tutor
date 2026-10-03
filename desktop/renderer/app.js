(() => {
  "use strict";

  const MAX_PAGES = 5;
  const byId = (id) => document.getElementById(id);
  const ui = Object.fromEntries([
    "class-picker", "class-name", "create-class", "document-picker", "library-detail",
    "delete-document", "delete-class", "backup-library", "search-scope",
    "search-status", "prepare-search", "cancel-search",
    "citation-preview", "citation-location", "citation-text",
    "account-status", "account-status-text", "account-identity", "account-detail",
    "sign-in", "cancel-sign-in", "sign-out", "open-usage", "notice",
    "choose-file", "use-sample", "document-empty", "document-info", "document-name",
    "document-meta", "sample-badge", "page-section", "page-options", "selection-count",
    "question", "model", "ask", "cancel-ask", "ask-help", "answer-section",
    "answer-badge", "answer-empty", "answer-progress", "answer-content", "answer-alert",
    "answer-prose", "citations-section", "citations", "gaps-section", "gaps",
  ].map((id) => [id, byId(id)]));
  const bridge = window.gct;
  let state = {};
  let selectedPages = new Set();
  let documentKey = "";
  let selectedModel = "";
  let localAction = null;
  let localNotice = "";
  let stateEvents = 0;
  let unsubscribe = null;
  let pageRows = [];
  let answerSuppressed = false;
  let resultKey = "";
  let searchScope = "class";

  const text = (value) => typeof value === "string" ? value : "";
  const connected = () => state.session?.status === "connected";
  const activeTask = () => state.busy
    ? state.busy
    : state.session?.status === "connecting" ? "signin"
      : localAction === "chooseFile" || localAction === "useSample" ? "document" : localAction;
  const isLocked = () => Boolean(activeTask());
  const make = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const validPages = () => {
    const seen = new Set();
    return (Array.isArray(state.document?.pages) ? state.document.pages : []).filter((page) => {
      const number = page?.page_or_slide;
      if (!Number.isInteger(number) || number < 1 || seen.has(number)) return false;
      seen.add(number);
      return true;
    });
  };
  const availableModels = () => {
    const seen = new Set();
    return (Array.isArray(state.models) ? state.models : []).filter((model) => {
      if (!text(model?.slug) || seen.has(model.slug)) return false;
      seen.add(model.slug);
      return true;
    });
  };


  const effectiveScope = () => !state.library || state.document?.sample ? "pages" : searchScope;
  const hasMaterial = () => effectiveScope() === "class"
    ? Boolean(state.library?.selectedClassId && state.library.documents?.length)
    : Boolean(state.document && (effectiveScope() !== "pages" || selectedPages.size));

  function renderLibrary() {
    const library = state.library;
    const locked = !bridge || !library || isLocked();
    const fill = (node, entries, value, placeholder, label) => {
      node.replaceChildren();
      const empty = make("option", "", placeholder); empty.value = ""; node.append(empty);
      for (const [index, entry] of (entries || []).entries()) {
        const option = make("option", "", label(entry, index)); option.value = entry.id; node.append(option);
      }
      node.value = value || "";
    };
    fill(ui["class-picker"], library?.classes, library?.selectedClassId, "Choose a class", entry => text(entry.name));
    fill(ui["document-picker"], library?.documents, library?.selectedDocumentId, "Choose saved material", (entry, index) => `${index + 1}. ${text(entry.filename)} · ${entry.page_count} pages`);
    ui["class-picker"].disabled = locked;
    ui["class-name"].disabled = locked;
    ui["create-class"].disabled = locked || !ui["class-name"].value.trim();
    ui["document-picker"].disabled = locked || !library?.selectedClassId;
    ui["delete-document"].disabled = locked || !library?.selectedDocumentId;
    ui["delete-class"].disabled = locked || !library?.selectedClassId;
    ui["backup-library"].disabled = locked;
    const preparing = activeTask() === "prepare_search";
    const search = library?.search;
    ui["search-status"].textContent = preparing
      ? "Preparing semantic search on this laptop. You can cancel; saved documents stay intact."
      : text(search?.message);
    ui["prepare-search"].disabled = locked || !library?.documents?.length || !search ||
      search.state === "unavailable" || search.state === "ready";
    ui["cancel-search"].hidden = !preparing;
    ui["cancel-search"].disabled = localAction === "cancel";
    ui["library-detail"].textContent = !library ? "Loading your local library…"
      : !library.classes?.length ? "Create a class, then add a PDF or PowerPoint. Files stay on this laptop."
        : `${library.documents?.length || 0} saved documents in this class. Your library stays when you sign out.`;
    ui["search-scope"].value = effectiveScope();
    ui["search-scope"].disabled = locked || state.document?.sample === true;
    ui["citation-preview"].hidden = !state.citation || answerSuppressed;
    ui["citation-location"].textContent = state.citation ? `${state.citation.filename} · page or slide ${state.citation.page_or_slide}` : "";
    ui["citation-text"].textContent = text(state.citation?.text);
  }

  function renderAccount() {
    const session = state.session || {};
    const signedIn = connected();
    const connecting = activeTask() === "signin";
    ui["account-status"].className = `status-line ${signedIn ? "connected" : connecting ? "connecting" : ""}`;
    ui["account-status-text"].textContent = signedIn ? "Connected" : connecting ? "Connecting…" : session.status === "reauth_required" ? "Reconnect required" : "Not connected";
    const identity = text(session.identity?.name) || text(session.identity?.email) || text(session.profileLabel);
    ui["account-identity"].textContent = identity;
    ui["account-identity"].hidden = !signedIn || !identity;
    ui["account-detail"].textContent = connecting
      ? "Complete the sign-in steps in the window that opened."
      : signedIn && session.sharing === true
        ? "Questions use your connected ChatGPT plan and its available usage."
        : signedIn
          ? "Your account is connected. ChatGPT access must be enabled before asking."
          : session.status === "reauth_required"
            ? "Reconnect your ChatGPT account to continue asking questions."
            : "Connect your account to ask questions using your ChatGPT plan.";
    ui["sign-in"].hidden = signedIn && session.sharing === true;
    ui["sign-in"].textContent = signedIn ? "Enable ChatGPT plan usage" : session.status === "reauth_required" ? "Reconnect with ChatGPT" : "Continue with ChatGPT";
    ui["sign-in"].disabled = !bridge || isLocked();
    ui["cancel-sign-in"].hidden = !connecting;
    ui["cancel-sign-in"].disabled = localAction === "cancel";
    ui["sign-out"].hidden = !signedIn;
    ui["sign-out"].disabled = isLocked();
    ui["open-usage"].disabled = !bridge || isLocked();
  }

  function renderModels() {
    const models = availableModels();
    if (!models.some((model) => model.slug === selectedModel)) selectedModel = models[0]?.slug || "";
    ui.model.replaceChildren();
    if (!models.length) {
      const empty = make("option", "", connected() ? "No models available" : "Connect to see your models");
      empty.value = "";
      ui.model.append(empty);
    }
    for (const model of models) {
      const option = make("option", "", text(model.display_name) || model.slug);
      option.value = model.slug;
      ui.model.append(option);
    }
    ui.model.value = selectedModel;
    ui.model.disabled = isLocked() || !connected() || !models.length;
  }

  function renderDocument() {
    const material = state.document;
    const pages = validPages();
    const key = material ? JSON.stringify([material.id, material.filename, material.sample === true, material.page_count, pages]) : "";
    if (key !== documentKey) {
      documentKey = key;
      if (material?.sample === true && !ui.question.value.trim() && text(material.suggested_question)) {
        ui.question.value = material.suggested_question;
      }
      selectedPages = new Set(pages.slice(0, MAX_PAGES).map((page) => page.page_or_slide));
      ui["page-options"].replaceChildren(make("legend", "sr-only", "Select up to five pages or slides"));
      pageRows = [];
      const slide = /\.pptx$/i.test(text(material?.filename));
      for (const page of pages) {
        const label = make("label", "page-option");
        const checkbox = make("input");
        checkbox.type = "checkbox";
        checkbox.value = String(page.page_or_slide);
        const content = make("span");
        content.append(make("span", "page-label", `${slide ? "Slide" : "Page"} ${page.page_or_slide}`));
        const excerpt = text(page.text).replace(/\s+/g, " ").trim();
        content.append(make("span", "page-excerpt", excerpt || (page.truncated ? "Preview omitted. This page can still be selected." : "No extractable text on this page.")));
        label.append(checkbox, content);
        checkbox.addEventListener("change", () => {
          if (isLocked()) { renderPageSelection(); return; }
          if (checkbox.checked && selectedPages.size < MAX_PAGES) selectedPages.add(page.page_or_slide);
          else selectedPages.delete(page.page_or_slide);
          answerSuppressed = true;
          renderPageSelection();
          renderControls();
          renderAnswer();
        });
        ui["page-options"].append(label);
        pageRows.push({ number: page.page_or_slide, checkbox, label });
      }
    }
    ui["document-empty"].hidden = Boolean(material);
    ui["document-info"].hidden = !material;
    ui["document-name"].textContent = text(material?.filename) || "Selected document";
    const count = Number.isInteger(material?.page_count) ? material.page_count : pages.length;
    const unit = /\.pptx$/i.test(text(material?.filename)) ? "slide" : "page";
    ui["document-meta"].textContent = `${count} ${unit}${count === 1 ? "" : "s"} · Opened locally`;
    ui["sample-badge"].hidden = material?.sample !== true;
    ui["page-section"].hidden = !material || effectiveScope() !== "pages";
    ui["choose-file"].textContent = state.library ? "Add a file to class" : material ? "Choose another file" : "Choose a file";
    renderPageSelection();
  }

  function renderPageSelection() {
    ui["selection-count"].textContent = `${selectedPages.size} of ${MAX_PAGES} selected`;
    for (const { number, checkbox, label } of pageRows) {
      checkbox.checked = selectedPages.has(number);
      checkbox.disabled = isLocked() || effectiveScope() !== "pages" || (!checkbox.checked && selectedPages.size >= MAX_PAGES);
      label.classList.toggle("selected", checkbox.checked);
      label.classList.toggle("unavailable", checkbox.disabled && !checkbox.checked);
    }
  }

  function renderControls() {
    const busy = activeTask();
    const hasQuestion = Boolean(ui.question.value.trim());
    const mayAsk = Boolean(bridge && connected() && state.session.sharing === true && selectedModel && hasMaterial() && hasQuestion && !busy);
    ui["choose-file"].disabled = !bridge || Boolean(busy) || (state.library && !state.library.selectedClassId);
    ui["use-sample"].disabled = !bridge || Boolean(busy);
    ui.question.disabled = !bridge || Boolean(busy);
    ui.ask.disabled = !mayAsk;
    ui["cancel-ask"].hidden = busy !== "ask";
    ui["cancel-ask"].disabled = localAction === "cancel";
    ui["ask-help"].textContent = !bridge ? "Open this preview in the desktop app to connect and choose material."
      : busy === "ask" ? "Your question and selected passages are being processed."
        : busy === "prepare_search" ? "Preparing local search. Cancel preparation to ask using keyword search."
        : busy === "document" ? "Reading document…"
        : busy === "signin" ? "Finish connecting your account to continue."
          : !connected() ? "Connect your ChatGPT account to get started."
            : state.session.sharing !== true ? "ChatGPT access is not enabled for this connection."
              : !selectedModel ? "Your account has no available models. Reconnect to refresh the list."
                : !hasMaterial() ? "Add material to this class, choose a document, or use the sample."
                  : effectiveScope() === "pages" && !selectedPages.size ? "Select at least one page or slide."
                    : !hasQuestion ? "Write a question about the selected passages."
                      : state.document?.sample ? "Asking about sample demo content using your ChatGPT plan."
                        : effectiveScope() !== "pages" ? "Only the matching passages and your question are sent when you ask."
                        : `Only the ${selectedPages.size} selected ${selectedPages.size === 1 ? "page or slide" : "pages or slides"} and your question will be sent.`;
  }

  function addParagraphs(value) {
    for (const paragraph of text(value).split(/\n\s*\n/).filter((part) => part.trim())) {
      ui["answer-prose"].append(make("p", "", paragraph.trim()));
    }
  }

  function renderAnswer() {
    const busy = activeTask() === "ask";
    ui["citation-preview"].hidden = !state.citation || answerSuppressed || busy;
    const result = answerSuppressed ? null : state.result;
    ui["answer-section"].setAttribute("aria-busy", String(busy));
    ui["answer-progress"].hidden = !busy;
    ui["answer-empty"].hidden = busy || Boolean(result);
    ui["answer-content"].hidden = busy || !result;
    ui["answer-badge"].hidden = busy || !result;
    ui["answer-prose"].replaceChildren();
    ui.citations.replaceChildren();
    ui.gaps.replaceChildren();
    ui["answer-alert"].hidden = true;
    ui["citations-section"].hidden = true;
    ui["gaps-section"].hidden = true;
    if (busy || !result) return;
    const kind = text(result.state).toUpperCase();
    const flagged = kind === "INTEGRITY_FLAGGED" || result.integrity?.ok === false;
    const failed = kind === "ERROR" || Boolean(result.error);
    const partial = kind === "PARTIAL" || result.coverage?.complete === false;
    const refused = kind === "REFUSAL";
    const cancelled = kind === "CANCELLED" || kind === "CANCELED";
    const citations = (Array.isArray(result.citations) ? result.citations : []).filter((citation) => text(citation?.label) && text(citation?.file) && Number.isInteger(citation.page_or_slide) && citation.page_or_slide > 0);
    const missingCitations = kind === "GROUNDED" && citations.length === 0;
    const warning = flagged || missingCitations || (partial && !refused);
    ui["answer-badge"].className = `answer-badge ${failed ? "error" : warning ? "warning" : refused || cancelled ? "neutral" : ""}`;
    ui["answer-badge"].textContent = failed ? "Couldn’t complete" : flagged ? "Needs review" : refused ? "Not supported" : cancelled ? "Cancelled" : missingCitations ? "Check sources" : partial ? "Partial support" : kind === "GROUNDED" ? "Grounded answer" : "Response";
    let alert = "";
    if (failed) alert = text(result.error?.message) || text(result.error) || "The answer could not be completed. Check your connection and try again.";
    else if (flagged) {
      const reasons = Array.isArray(result.integrity?.reasons) ? result.integrity.reasons.map(text).filter(Boolean) : [];
      alert = ["This response needs review. Check its claims against your material.", ...reasons].join("\n");
    } else if (refused) alert = effectiveScope() === "pages"
      ? "The selected passages don’t support an answer to this question. Try different pages or a more focused question."
      : "The matching passages don’t support an answer. Search may miss related wording; rephrase your question or select relevant pages.";
    else if (cancelled) alert = "The request was cancelled. You can change your selection and ask again.";
    else if (missingCitations) alert = "No usable source references were returned. Check this response against your material.";
    else if (partial) alert = "The selected passages support only part of the answer. Review the gaps below.";
    if (alert) {
      ui["answer-alert"].textContent = alert;
      ui["answer-alert"].className = `answer-alert ${failed ? "error" : refused || cancelled ? "neutral" : ""}`;
      ui["answer-alert"].hidden = false;
    }
    if (!failed && !cancelled) addParagraphs(result.answer_prose);
    if (!failed && !cancelled && citations.length) {
      for (const citation of citations) {
        const item = make("li", "citation");
        item.append(make("span", "citation-label", citation.label), make("span", "", `${citation.file} · ${/\.pptx$/i.test(citation.file) ? "slide" : "p."} ${citation.page_or_slide}`));
        if (state.library && !state.document?.sample && text(citation.chunk_id)) {
          const open = make("button", "text-button", "View saved passage");
          open.type = "button"; open.disabled = isLocked();
          open.addEventListener("click", () => perform("showCitation", { chunkId: citation.chunk_id }));
          item.append(open);
        }
        ui.citations.append(item);
      }
      ui["citations-section"].hidden = false;
    }
    const gaps = Array.isArray(result.coverage?.gaps) ? result.coverage.gaps.map((gap) => text(gap) || text(gap?.description) || text(gap?.text)).filter(Boolean) : [];
    if (!failed && !cancelled && gaps.length) {
      for (const gap of gaps) ui.gaps.append(make("li", "", gap));
      ui["gaps-section"].hidden = false;
    }
  }

  function render() {
    renderLibrary();
    renderAccount();
    renderModels();
    renderDocument();
    renderControls();
    renderAnswer();
    const notice = localNotice || text(state.notice);
    ui.notice.textContent = notice;
    ui.notice.className = `notice${localNotice ? " error" : ""}`;
    ui.notice.hidden = !notice;
  }

  function acceptState(next) {
    if (!next || typeof next !== "object" || Array.isArray(next)) return;
    const nextResultKey = JSON.stringify(next.result || null);
    state = next;
    if (nextResultKey !== resultKey || !next.result) answerSuppressed = false;
    resultKey = nextResultKey;
    localNotice = "";
    render();
  }

  async function perform(name, args, action = name) {
    if (!bridge || typeof bridge[name] !== "function") return;
    const version = stateEvents;
    localAction = action;
    localNotice = "";
    if (name === "ask" || name === "chooseFile" || name === "useSample" || name === "signOut" || ["selectClass", "selectStoredDocument", "deleteDocument", "deleteClass", "createClass"].includes(name)) answerSuppressed = true;
    render();
    try {
      const next = await (args === undefined ? bridge[name]() : bridge[name](args));
      if (stateEvents === version && next && typeof next === "object") acceptState(next);
    } catch {
      localNotice = "That action couldn’t finish. Please try again.";
    } finally {
      if (localAction === action) localAction = null;
      render();
    }
  }

  ui["class-name"].addEventListener("input", renderLibrary);
  ui["create-class"].addEventListener("click", async () => {
    if (ui["create-class"].disabled) return;
    await perform("createClass", { name: ui["class-name"].value.trim() });
    ui["class-name"].value = ""; renderLibrary();
  });
  ui["class-picker"].addEventListener("change", () => {
    if (!isLocked() && ui["class-picker"].value) perform("selectClass", { classId: ui["class-picker"].value });
    else renderLibrary();
  });
  ui["document-picker"].addEventListener("change", () => {
    if (!isLocked() && ui["document-picker"].value) perform("selectStoredDocument", { documentId: ui["document-picker"].value });
    else renderLibrary();
  });
  ui["delete-document"].addEventListener("click", () => perform("deleteDocument"));
  ui["delete-class"].addEventListener("click", () => perform("deleteClass"));
  ui["backup-library"].addEventListener("click", () => perform("backupLibrary"));
  ui["prepare-search"].addEventListener("click", () => {
    if (!ui["prepare-search"].disabled) perform("prepareSearch", undefined, "prepare_search");
  });
  ui["cancel-search"].addEventListener("click", () => perform("cancelAsk", undefined, "cancel"));
  ui["search-scope"].addEventListener("change", () => {
    if (isLocked() || !["class", "document", "pages"].includes(ui["search-scope"].value)) return;
    searchScope = ui["search-scope"].value; answerSuppressed = true; render();
  });
  ui["sign-in"].addEventListener("click", () => perform("signIn", undefined, "signin"));
  ui["cancel-sign-in"].addEventListener("click", () => perform("cancelSignIn", undefined, "cancel"));
  ui["sign-out"].addEventListener("click", () => perform("signOut"));
  ui["choose-file"].addEventListener("click", () => perform("chooseFile"));
  ui["use-sample"].addEventListener("click", () => perform("useSample"));
  ui["open-usage"].addEventListener("click", () => perform("openUsage"));
  ui["cancel-ask"].addEventListener("click", () => perform("cancelAsk", undefined, "cancel"));
  ui.question.addEventListener("input", () => { answerSuppressed = true; renderControls(); renderAnswer(); });
  ui.model.addEventListener("change", () => { selectedModel = ui.model.value; answerSuppressed = true; renderControls(); renderAnswer(); });
  ui.ask.addEventListener("click", () => {
    if (ui.ask.disabled) return;
    const request = { question: ui.question.value.trim(), model: selectedModel };
    if (state.library && !state.document?.sample) request.scope = effectiveScope();
    if (effectiveScope() === "pages") request.pages = [...selectedPages].sort((a, b) => a - b);
    perform("ask", request, "ask");
  });
  if (bridge && typeof bridge.onState === "function") {
    unsubscribe = bridge.onState((next) => { stateEvents += 1; acceptState(next); });
  }
  window.addEventListener("beforeunload", () => { if (typeof unsubscribe === "function") unsubscribe(); });
  render();
  if (bridge) {
    const version = stateEvents;
    Promise.resolve().then(() => bridge.getState()).then((initial) => {
      if (version === stateEvents) acceptState(initial);
    }).catch(() => { localNotice = "The app couldn’t load its current state. Close this preview and open it again."; render(); });
  }
})();
