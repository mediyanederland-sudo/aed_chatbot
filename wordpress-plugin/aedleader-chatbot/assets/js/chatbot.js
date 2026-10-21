/**
 * AED Leader AI Chatbot — Widget
 *
 * Reads config from window.AEDChatbotConfig.
 * Vanilla JS IIFE — no dependencies.
 */

(function () {
    'use strict';

    function init() {

    var cfg = window.AEDChatbotConfig || {};
    if (cfg.backendUrl === undefined || cfg.backendUrl === null) return;
    // Empty string = same origin (works on any port/domain)

    //  Apply theme from config
    (function applyTheme() {
        var root = document.getElementById('aed-chatbot-root');
        if (!root) return;
        if (cfg.primaryColor) {
            root.style.setProperty('--aed-primary', cfg.primaryColor);
            root.style.setProperty('--aed-bg-user', cfg.primaryColor);
        }
        if (cfg.accentColor) {
            root.style.setProperty('--aed-accent', cfg.accentColor);
        }
        if (cfg.zIndex) {
            root.style.setProperty('--aed-z', cfg.zIndex);
        }
        if (cfg.position === 'left') {
            root.classList.add('aed-pos-left');
        }
    })();

    //  Header status text override (for trial/beta/online label)
    // cfg.betaNotice unset → keep static HTML default
    // cfg.betaNotice string → override visible text
    // cfg.betaNotice false / "" → hide the entire status row
    (function applyStatusText() {
        if (cfg.betaNotice === undefined) return;
        var textEl = document.getElementById('aed-header-status-text');
        if (!textEl) return;
        if (cfg.betaNotice === false || cfg.betaNotice === '') {
            var row = textEl.closest('.aed-header-status');
            if (row) row.style.display = 'none';
        } else {
            textEl.textContent = String(cfg.betaNotice);
        }
    })();

    //  DOM refs
    var toggleBtn  = document.getElementById('aed-chat-toggle');
    var panel      = document.getElementById('aed-chat-panel');
    var messagesEl = document.getElementById('aed-messages');
    var typingEl   = document.getElementById('aed-typing');
    var inputEl    = document.getElementById('aed-input');
    var sendBtn    = document.getElementById('aed-send');
    var closeBtn   = document.getElementById('aed-close');
    var chipsEl    = document.getElementById('aed-chips');
    var resetBtn   = document.getElementById('aed-reset');
    var teaserEl   = document.getElementById('aed-teaser');
    var debugPanel = document.getElementById('aed-debug-panel');
    var debugContent = document.getElementById('aed-debug-content');
    var debugSession = document.getElementById('aed-debug-session');
    var debugToggleBtn = document.getElementById('aed-debug-toggle');
    var debugVisible = false;

    if (!toggleBtn || !panel || !messagesEl) return;

    //  State
    var sessionId      = localStorage.getItem('aed_chat_session_id') || null;
    var sessionToken   = localStorage.getItem('aed_chat_session_token') || null;
    var isOpen         = false;
    var greeted        = false;
    var openedTracked  = false;  // fire widget_opened only once per page load
    var hasConversation = false;
    var lastUserMessage = '';
    var messages        = []; // Local transcript cache.
    var botMsgSeq       = 0;

    // Synthetic ID for chip clicks that fire BEFORE any /chat exchange
    // (user opens widget, clicks Inquiry, never types). Stored in
    // localStorage so a reload preserves the same pre-session id; promoted
    // to the real session_id once /chat returns one and saved over.
    function ensureTrackingId() {
        if (sessionId) return sessionId;
        var pre = localStorage.getItem('aed_pre_session_id');
        if (!pre) {
            // 16 hex chars — fits the alphanumeric+_+- shape the backend
            // accepts, well under the 64-char cap.
            var rnd = '';
            for (var i = 0; i < 16; i++) rnd += Math.floor(Math.random() * 16).toString(16);
            pre = 'pre_' + rnd;
            localStorage.setItem('aed_pre_session_id', pre);
        }
        return pre;
    }

    // Throwaway attribution token for outbound product links (like gclid).
    // NEVER the session id — session identifiers don't belong in URLs
    // (OWASP). The cid->session pair is logged server-side with each
    // link_click event; WooCommerce Order Attribution carries the cid back
    // on the order, and the backend resolves it to the conversation.
    function ensureCid() {
        var cid = localStorage.getItem('aed_cid');
        if (!cid) {
            var rnd = '';
            for (var i = 0; i < 16; i++) rnd += Math.floor(Math.random() * 16).toString(16);
            cid = 'cid_' + rnd;
            localStorage.setItem('aed_cid', cid);
        }
        return cid;
    }

    // Load stored messages
    try {
        var stored = localStorage.getItem('aed_chat_messages');
        if (stored) {
            messages = JSON.parse(stored);
            // Discard stored greetings when no user conversation exists.
            var hasUserMsg = messages.some(function (m) { return m && m.type === 'user'; });
            if (!hasUserMsg) {
                messages = [];
                localStorage.removeItem('aed_chat_messages');
            }
            if (messages.length > 0) {
                hasConversation = true;
                greeted = true;
                messages.forEach(function (m) { renderStoredMessage(m); });
                if (chipsEl) chipsEl.classList.add('aed-hidden');
                if (resetBtn) resetBtn.classList.add('aed-visible');
            }
        }
    } catch (e) { /* ignore corrupt storage */ }

    //  Toggle open/close
    function openPanel(isAuto) {
        isOpen = true;
        // Engagement: count a USER-initiated open once per page load. Auto-open
        // (cfg.autoOpen) is excluded — it isn't the user choosing to engage.
        if (!isAuto && !openedTracked) {
            openedTracked = true;
            trackEvent('widget_opened');
        }
        panel.hidden = false;
        if (teaserEl) teaserEl.hidden = true;   // teaser and panel never coexist
        toggleBtn.setAttribute('aria-expanded', 'true');
        toggleBtn.querySelector('.aed-icon-chat').style.display = 'none';
        toggleBtn.querySelector('.aed-icon-close').style.display = '';
        inputEl.focus();

        if (!greeted) {
            greeted = true;
            var greetText = cfg.greeting || "Hi, I'm Rescue Rita — your AED Leader assistant. I can help you find the right AED for your needs.\n\nWhat are you looking for today?";
            addBotMessage(greetText, null, null, null, true);
        }
        scrollToBottom();
    }

    function closePanel() {
        isOpen = false;
        panel.hidden = true;
        toggleBtn.setAttribute('aria-expanded', 'false');
        toggleBtn.querySelector('.aed-icon-chat').style.display = '';
        toggleBtn.querySelector('.aed-icon-close').style.display = 'none';
    }

    toggleBtn.addEventListener('click', function () { isOpen ? closePanel() : openPanel(); });
    if (closeBtn) closeBtn.addEventListener('click', closePanel);








    if (teaserEl) {
        var teaserTextEl = teaserEl.querySelector('.aed-teaser-text');
        if (teaserTextEl && cfg.teaserText) {
            teaserTextEl.textContent = String(cfg.teaserText);
        }
        var teaserBody = teaserEl.querySelector('.aed-teaser-body');
        if (teaserBody) {
            teaserBody.addEventListener('click', function () {
                teaserEl.hidden = true;
                if (!isOpen) openPanel();
            });
            teaserBody.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    teaserEl.hidden = true;
                    if (!isOpen) openPanel();
                }
            });
        }
        var teaserDismiss = teaserEl.querySelector('.aed-teaser-dismiss');
        if (teaserDismiss) {
            teaserDismiss.addEventListener('click', function (e) {
                e.stopPropagation();
                teaserEl.hidden = true;
                try { localStorage.setItem('aed_teaser_dismissed', '1'); } catch (e2) { /* ignore */ }
            });
        }
    }

    // Show the teaser ~3s after load — only if enabled, not previously
    // dismissed, no active conversation, and the panel isn't already open.
    if (cfg.showTeaser && teaserEl && !hasConversation
            && localStorage.getItem('aed_teaser_dismissed') !== '1') {
        setTimeout(function () {
            if (!isOpen && !hasConversation) teaserEl.hidden = false;
        }, 3000);
    }

    //  Debug panel toggle
    function toggleDebug() {
        debugVisible = !debugVisible;
        if (debugPanel) debugPanel.hidden = !debugVisible;
        if (debugToggleBtn) debugToggleBtn.classList.toggle('aed-active', debugVisible);
    }

    // Bind debug controls only when the debug toggle exists; production templates omit it.
    if (debugToggleBtn) {
        debugToggleBtn.addEventListener('click', toggleDebug);

        document.addEventListener('keydown', function (e) {
            if (e.ctrlKey && e.key === 'd') {
                e.preventDefault();
                toggleDebug();
            }
        });
    }

    function updateDebug(data) {
        if (!debugContent) return;
        var d = data.debug || {};

        if (debugSession) {
            debugSession.textContent = 'Session: ' + (data.session_id || '—').slice(0, 8) + '...';
        }

        // Log full response to console for deep debugging
        console.group('%c[AED Chat] Turn ' + (d.turn || '?'), 'color: #7c8aff; font-weight: bold');
        console.log('Intent:', d.intent);
        console.log('Journey:', d.journey_stage);
        console.log('Query:', d.search_query);
        console.log('Filters requested:', d.filters);
        console.log('Filters used:', d.filters_used);
        console.log('Relaxed:', d.was_relaxed);
        console.log('Results:', d.total_found);
        console.log('Hallucinated blocked:', d.hallucinated);
        console.log('Time:', d.time, 's');
        console.log('Products:', (data.product_cards || []).length);
        console.log('Upsells:', (data.upsell_cards || []).length);
        console.log('Follow-up:', data.follow_up_question);
        console.log('Full response:', data);
        console.groupEnd();

        var html = '';
        html += '<div class="aed-debug-turn">';

        // Turn header
        html += '<div class="aed-debug-turn-header">';
        html += '<span class="aed-debug-label">Turn ' + (d.turn || '?') + '</span>';
        html += '<span class="aed-debug-time">' + (d.time ? d.time.toFixed(2) + 's' : '—') + '</span>';
        html += '</div>';

        // Intent
        html += '<div class="aed-debug-row">';
        html += '<span class="aed-debug-key">Intent</span>';
        html += '<span class="aed-debug-val aed-intent">' + escapeHtml(d.intent || '—') + '</span>';
        html += '</div>';

        // Journey stage
        html += '<div class="aed-debug-row">';
        html += '<span class="aed-debug-key">Journey</span>';
        var stageClass = {'awareness':'aed-warn','consideration':'','decision':'aed-good','retention':'aed-good'}[d.journey_stage] || '';
        html += '<span class="aed-debug-val ' + stageClass + '">' + escapeHtml(d.journey_stage || '—') + '</span>';
        html += '</div>';

        // Configurator info
        if (d.configurator) {
            var c = d.configurator;
            html += '<div class="aed-debug-row">';
            html += '<span class="aed-debug-key">Config</span>';
            var parts = [];
            if (c.environment) parts.push('env=' + c.environment);
            if (c.budget) parts.push('budget=' + c.budget);
            if (c.audience) parts.push('audience=' + c.audience);
            if (c.pediatric !== null && c.pediatric !== undefined) parts.push('pediatric=' + c.pediatric);
            if (c.package_preference) parts.push('package=' + c.package_preference);
            html += '<span class="aed-debug-val aed-debug-filters">' + escapeHtml(parts.join(', ') || '(none)') + '</span>';
            html += '</div>';
            if (c.missing_fields && c.missing_fields.length > 0) {
                html += '<div class="aed-debug-row">';
                html += '<span class="aed-debug-key">Missing</span>';
                html += '<span class="aed-debug-val aed-warn">' + escapeHtml(c.missing_fields.join(', ')) + '</span>';
                html += '</div>';
            }
            html += '<div class="aed-debug-row">';
            html += '<span class="aed-debug-key">Ready</span>';
            html += '<span class="aed-debug-val ' + (c.ready_to_search ? 'aed-good' : 'aed-warn') + '">' + (c.ready_to_search ? 'YES — searching' : 'NO — gathering info') + '</span>';
            html += '</div>';
        }

        // Search query
        html += '<div class="aed-debug-row">';
        html += '<span class="aed-debug-key">Query</span>';
        html += '<span class="aed-debug-val">' + escapeHtml(d.search_query || '—') + '</span>';
        html += '</div>';

        // Filters requested
        var filtersReq = d.filters ? JSON.stringify(d.filters) : '—';
        html += '<div class="aed-debug-row">';
        html += '<span class="aed-debug-key">Filters</span>';
        html += '<span class="aed-debug-val aed-debug-filters">' + escapeHtml(filtersReq) + '</span>';
        html += '</div>';

        // Filters actually used
        if (d.filters_used) {
            var filtersUsed = JSON.stringify(d.filters_used);
            html += '<div class="aed-debug-row">';
            html += '<span class="aed-debug-key">Used</span>';
            html += '<span class="aed-debug-val aed-debug-filters">' + escapeHtml(filtersUsed) + '</span>';
            html += '</div>';
        }

        // Was relaxed
        if (d.was_relaxed) {
            html += '<div class="aed-debug-row">';
            html += '<span class="aed-debug-key">Relaxed</span>';
            html += '<span class="aed-debug-val aed-warn">Yes — filters were loosened</span>';
            html += '</div>';
        }

        // Results count
        html += '<div class="aed-debug-row">';
        html += '<span class="aed-debug-key">Found</span>';
        var foundClass = (d.total_found || 0) > 0 ? 'aed-good' : 'aed-bad';
        html += '<span class="aed-debug-val ' + foundClass + '">' + (d.total_found || 0) + ' products</span>';
        html += '</div>';

        // Products shown
        html += '<div class="aed-debug-row">';
        html += '<span class="aed-debug-key">Shown</span>';
        html += '<span class="aed-debug-val">' + (data.product_cards || []).length + ' cards + ' + (data.upsell_cards || []).length + ' upsells</span>';
        html += '</div>';

        // Hallucinated
        if (d.hallucinated > 0) {
            html += '<div class="aed-debug-row">';
            html += '<span class="aed-debug-key">Blocked</span>';
            html += '<span class="aed-debug-val aed-bad">' + d.hallucinated + ' hallucinated IDs</span>';
            html += '</div>';
        }

        // Follow-up
        if (data.follow_up_question) {
            html += '<div class="aed-debug-row">';
            html += '<span class="aed-debug-key">Follow-up</span>';
            html += '<span class="aed-debug-val">' + escapeHtml(data.follow_up_question) + '</span>';
            html += '</div>';
        }

        html += '</div>';

        // Prepend new turn at top
        debugContent.innerHTML = html + debugContent.innerHTML.replace('<div class="aed-debug-empty">Send a message to see pipeline trace</div>', '');
    }

    //  Suggestion chips
    // Data-driven from cfg.chips. Three action types:
    //   prompt — fill input + sendMessage (existing behavior)
    //   url    — open external link in new tab
    //   info   — render a structured info card as a bot message (no /chat)
    // Every action also fires a /track event so admin can see CTR per chip.
    function renderChips() {
        if (!chipsEl) return;
        chipsEl.innerHTML = '';
        var chips = (cfg && Array.isArray(cfg.chips)) ? cfg.chips : [];
        chips.forEach(function (chip) {
            if (!chip || !chip.id || !chip.label || !chip.action) return;
            var btn = document.createElement('button');
            btn.className = 'aed-chip-btn';
            btn.type = 'button';
            btn.textContent = chip.label;
            btn.setAttribute('data-chip-id', chip.id);
            btn.addEventListener('click', function () { handleChipAction(chip); });
            chipsEl.appendChild(btn);
        });
    }

    function handleChipAction(chip) {
        // Fire-and-forget telemetry — never blocks the UX path.
        trackEvent('chip_click', chip.id);

        if (chip.action === 'prompt') {
            inputEl.value = String(chip.payload || '');
            sendMessage();
            return;
        }
        if (chip.action === 'url') {
            var url = chip.payload && chip.payload.url;
            if (!/^https?:\/\//.test(url || '')) return;
            window.open(url, chip.payload.target || '_blank', 'noopener');
            return;
        }
        if (chip.action === 'info') {
            addInfoCardMessage(chip);
            // Hide chips after the first interaction — same behavior as
            // sending a message. Keeps the conversation focused.
            if (!hasConversation) {
                hasConversation = true;
                if (chipsEl) chipsEl.classList.add('aed-hidden');
                if (resetBtn) resetBtn.classList.add('aed-visible');
            }
        }
    }

    // POST /track — fire-and-forget, swallows errors silently. NEVER
    // surface a tracking failure to the user; telemetry is best-effort.
    function trackEvent(eventName, chipId, metadata) {
        try {
            var body = {
                session_id: ensureTrackingId(),
                event_name: eventName,
                chip_id: chipId || null,
                metadata: metadata || {}
            };
            // Multisite: client-side hint; the backend stamps the authoritative
            // tenant from the Origin header regardless.
            if (cfg.site) { body.metadata.site = cfg.site; }
            // keepalive=true so the request still flies if the user
            // immediately navigates away (e.g. after clicking a url chip).
            fetch(cfg.backendUrl + '/track', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                keepalive: true
            }).catch(function () { /* swallow */ });
        } catch (e) { /* swallow */ }
    }

    // Track product links on new and restored cards through delegated events.
    if (messagesEl) {
        messagesEl.addEventListener('click', function (e) {
            var link = (e.target && e.target.closest) ? e.target.closest('.aed-btn-view') : null;
            if (!link) return;
            var pid = link.getAttribute('data-product-id') || '';
            trackEvent('link_click', pid || null, {
                target_type: 'product',
                url: link.getAttribute('href') || '',
                cid: ensureCid()
            });
        });

        // Delegated 👍/👎 feedback (CSAT). Locks after one vote per page view;
        // the aggregator also dedupes by (session, message index) keeping the
        // latest vote, so a reload-and-revote can't double-count.
        messagesEl.addEventListener('click', function (e) {
            var btn = (e.target && e.target.closest) ? e.target.closest('.aed-fb-btn') : null;
            if (!btn) return;
            var row = btn.closest('.aed-feedback');
            if (!row || row.getAttribute('data-voted') === '1') return;
            var rating = btn.getAttribute('data-fb') === 'down' ? 'feedback_down' : 'feedback_up';
            var turn = row.getAttribute('data-turn') || '';
            var content = btn.closest('.aed-msg-content');
            var bubble = content ? content.querySelector('.aed-msg-bubble') : null;
            var preview = bubble ? (bubble.textContent || '').trim().slice(0, 120) : '';
            trackEvent(rating, turn || null, { message_preview: preview });
            row.setAttribute('data-voted', '1');
            btn.classList.add('aed-fb-active');
            row.querySelectorAll('.aed-fb-btn').forEach(function (b) {
                if (b !== btn) b.classList.add('aed-fb-dim');
            });
        });
    }

    renderChips();

    // Engagement: the widget rendered on this page ("saw it"). One per load.
    trackEvent('widget_loaded');

    //  Reset button
    if (resetBtn) {
        resetBtn.addEventListener('click', function () {
            messagesEl.innerHTML = '';
            sessionId = null;
            sessionToken = null;
            localStorage.removeItem('aed_chat_session_id');
            localStorage.removeItem('aed_chat_session_token');
            localStorage.removeItem('aed_chat_messages');
            localStorage.removeItem('aed_pre_session_id');
            localStorage.removeItem('aed_cid');
            messages = [];
            hasConversation = false;
            greeted = false;
            renderChips();  // rebuild from config in case it changed
            if (chipsEl) chipsEl.classList.remove('aed-hidden');
            resetBtn.classList.remove('aed-visible');
            inputEl.disabled = false;
            sendBtn.disabled = false;
            greeted = true;
            var greetText = cfg.greeting || "Hi, I'm Rescue Rita — your AED Leader assistant. I can help you find the right AED for your needs.\n\nWhat are you looking for today?";
            addBotMessage(greetText, null, null, null, true);
            inputEl.focus();
        });
    }

    //  Input handling
    inputEl.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    sendBtn.addEventListener('click', function () { sendMessage(); });

    // Auto-resize textarea
    inputEl.addEventListener('input', function () {
        this.style.height = '38px';
        this.style.height = Math.min(this.scrollHeight, 100) + 'px';
    });

    //  Send message
    async function sendMessage() {
        var message = inputEl.value.trim();
        if (!message) return;

        inputEl.value = '';
        inputEl.style.height = '38px';
        lastUserMessage = message;

        addUserMessage(message);
        showTyping(true);
        sendBtn.disabled = true;

        if (!hasConversation) {
            hasConversation = true;
            // Engagement: first user message = a real chat started. Fires with
            // the pre_ id (sessionId isn't set until /chat returns), matching
            // the widget_loaded/opened ids for a clean funnel join.
            trackEvent('chat_started');
            if (chipsEl) chipsEl.classList.add('aed-hidden');
            if (resetBtn) resetBtn.classList.add('aed-visible');
        }

        try {
            var controller = new AbortController();
            var timeout = setTimeout(function () { controller.abort(); }, 30000);

            var resp = await fetch(cfg.backendUrl + '/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: message, session_id: sessionId, session_token: sessionToken, site: cfg.site || null }),
                signal: controller.signal
            });

            clearTimeout(timeout);

            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var data = await resp.json();

            sessionId = data.session_id;
            sessionToken = data.session_token;
            localStorage.setItem('aed_chat_session_id', sessionId);
            localStorage.setItem('aed_chat_session_token', sessionToken);
            // Once we have a real session, drop the pre_session marker so
            // future track events use the real id (joinable to chat history).
            localStorage.removeItem('aed_pre_session_id');

            showTyping(false);
            updateDebug(data);
            addBotMessage(
                data.message,
                data.product_cards,
                data.upsell_message,
                data.upsell_cards
            );

        } catch (err) {
            showTyping(false);
            addErrorMessage(err.name === 'AbortError' ? 'Request timed out. Please try again.' : 'Something went wrong. Please try again.');
        }

        sendBtn.disabled = false;
        inputEl.focus();
    }

    //  Render: user message
    function addUserMessage(text) {
        var el = document.createElement('div');
        el.className = 'aed-msg aed-msg-user';
        el.innerHTML = '<div class="aed-msg-bubble">' + escapeHtml(text) + '</div>';
        messagesEl.appendChild(el);
        saveMessage({ type: 'user', text: text });
        scrollToBottom();
    }

    //  Render: bot message with product cards and upsells
    // _skipPersist=true: render the message but do NOT save to localStorage.
    // Used for the auto-greeting which is regenerated from cfg.greeting on
    // every panel open — persisting it would falsely flag the session as
    // "in conversation" on page reload (hiding the chips).
    function addBotMessage(text, productCards, upsellMessage, upsellCards, _skipPersist) {
        var wrapper = document.createElement('div');
        wrapper.className = 'aed-msg aed-msg-bot';

        var avatarHtml = '<div class="aed-msg-avatar"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg></div>';
        var contentHtml = '<div class="aed-msg-content">';
        contentHtml += '<div class="aed-msg-bubble">' + renderMarkdown(text || '') + '</div>';

        // Product cards
        if (productCards && productCards.length > 0) {
            contentHtml += renderProductCards(productCards);
        }

        // Upsell section
        if (upsellCards && upsellCards.length > 0) {
            contentHtml += renderUpsellSection(upsellMessage, upsellCards);
        }

        // The reply message contains the guiding question; follow_up_question is not rendered.
        if (!_skipPersist) {
            contentHtml += renderFeedbackRow(++botMsgSeq);
        }

        contentHtml += '</div>';
        wrapper.innerHTML = avatarHtml + contentHtml;

        // Attach scroll arrow handlers for product cards
        attachScrollArrows(wrapper);

        messagesEl.appendChild(wrapper);
        if (!_skipPersist) {
            saveMessage({
                type: 'bot',
                text: text,
                productCards: productCards || [],
                upsellMessage: upsellMessage,
                upsellCards: upsellCards || []
            });
        }
        // Scroll to the text bubble (not absolute bottom) so answer is visible
        var bubble = wrapper.querySelector('.aed-msg-bubble');
        if (bubble) {
            requestAnimationFrame(function () { bubble.scrollIntoView({ behavior: 'smooth', block: 'start' }); });
        } else {
            scrollToBottom();
        }
    }

    //  Render: info card (Support / Inquiry chips)
    // Static info card rendered as a bot message — no /chat round-trip.
    // Shape: { title: str, lines: [str], cta?: { label, url } }
    // The CTA button, when clicked, fires a 'form_button_click' track event
    // so we can measure the inquiry funnel (chip click → form click).
    function addInfoCardMessage(chip) {
        var p = chip.payload || {};
        var wrapper = document.createElement('div');
        wrapper.className = 'aed-msg aed-msg-bot';

        var avatarHtml = '<div class="aed-msg-avatar"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg></div>';

        var html = '<div class="aed-msg-content"><div class="aed-info-card">';
        if (p.title) html += '<div class="aed-info-card-title">' + escapeHtml(p.title) + '</div>';
        if (Array.isArray(p.lines)) {
            html += '<div class="aed-info-card-body">';
            p.lines.forEach(function (line) {
                html += '<div class="aed-info-card-line">' + escapeHtml(line) + '</div>';
            });
            html += '</div>';
        }
        if (p.cta && p.cta.url && /^https?:\/\//.test(p.cta.url)) {
            html += '<a class="aed-info-card-cta" href="' + escapeAttr(p.cta.url) +
                    '" target="_blank" rel="noopener" data-chip-id="' + escapeAttr(chip.id) + '">' +
                    escapeHtml(p.cta.label || 'Open') +
                    '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M14 3v2h3.59L8.29 14.29l1.41 1.41L19 6.41V10h2V3z"/></svg>' +
                    '</a>';
        }
        html += '</div></div>';
        wrapper.innerHTML = avatarHtml + html;

        // Attach CTA click tracking
        var ctaEl = wrapper.querySelector('.aed-info-card-cta');
        if (ctaEl) {
            ctaEl.addEventListener('click', function () {
                trackEvent('form_button_click', chip.id, { url: p.cta.url });
            });
        }

        messagesEl.appendChild(wrapper);
        // Persist as a special message type so reload re-renders it
        saveMessage({ type: 'info_card', chip: { id: chip.id, payload: p } });
        scrollToBottom();
    }

    //  Render: error message
    function addErrorMessage(text) {
        var el = document.createElement('div');
        el.className = 'aed-msg aed-msg-bot';
        el.innerHTML =
            '<div class="aed-msg-avatar"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg></div>' +
            '<div class="aed-error">' +
                '<span>' + escapeHtml(text) + '</span>' +
                '<button class="aed-error-retry">Retry</button>' +
            '</div>';

        el.querySelector('.aed-error-retry').addEventListener('click', function () {
            el.remove();
            if (lastUserMessage) {
                inputEl.value = lastUserMessage;
                sendMessage();
            }
        });

        messagesEl.appendChild(el);
        scrollToBottom();
    }

    //  Render: stored messages on page load
    function attachScrollArrows(container) {
        container.querySelectorAll('.aed-products-wrapper').forEach(function (wrap) {
            var track = wrap.querySelector('.aed-products');
            var leftBtn = wrap.querySelector('.aed-scroll-left');
            var rightBtn = wrap.querySelector('.aed-scroll-right');
            if (track && leftBtn && rightBtn) {
                leftBtn.onclick = function () { track.scrollLeft -= 220; };
                rightBtn.onclick = function () { track.scrollLeft += 220; };
            }
        });
    }

    function renderStoredMessage(m) {
        if (m.type === 'user') {
            var el = document.createElement('div');
            el.className = 'aed-msg aed-msg-user';
            el.innerHTML = '<div class="aed-msg-bubble">' + escapeHtml(m.text) + '</div>';
            messagesEl.appendChild(el);
        } else if (m.type === 'info_card') {
            // Re-render an info card on reload — replay through addInfoCardMessage
            // would double-save AND re-fire telemetry, so inline the render here.
            if (m.chip && m.chip.payload) {
                var p = m.chip.payload;
                var w = document.createElement('div');
                w.className = 'aed-msg aed-msg-bot';
                var h = '<div class="aed-msg-avatar"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg></div>';
                h += '<div class="aed-msg-content"><div class="aed-info-card">';
                if (p.title) h += '<div class="aed-info-card-title">' + escapeHtml(p.title) + '</div>';
                if (Array.isArray(p.lines)) {
                    h += '<div class="aed-info-card-body">';
                    p.lines.forEach(function (line) { h += '<div class="aed-info-card-line">' + escapeHtml(line) + '</div>'; });
                    h += '</div>';
                }
                if (p.cta && p.cta.url && /^https?:\/\//.test(p.cta.url)) {
                    h += '<a class="aed-info-card-cta" href="' + escapeAttr(p.cta.url) + '" target="_blank" rel="noopener">' + escapeHtml(p.cta.label || 'Open') + '</a>';
                }
                h += '</div></div>';
                w.innerHTML = h;
                messagesEl.appendChild(w);
            }
        } else {
            // Re-render bot message without saving again
            var wrapper = document.createElement('div');
            wrapper.className = 'aed-msg aed-msg-bot';
            var avatarHtml = '<div class="aed-msg-avatar"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg></div>';
            var contentHtml = '<div class="aed-msg-content">';
            contentHtml += '<div class="aed-msg-bubble">' + renderMarkdown(m.text || '') + '</div>';
            if (m.productCards && m.productCards.length > 0) {
                contentHtml += renderProductCards(m.productCards);
            }
            if (m.upsellCards && m.upsellCards.length > 0) {
                contentHtml += renderUpsellSection(m.upsellMessage, m.upsellCards);
            }
            // Don't re-render follow-up chips for old messages
            contentHtml += renderFeedbackRow(++botMsgSeq);
            contentHtml += '</div>';
            wrapper.innerHTML = avatarHtml + contentHtml;

            // Attach scroll arrow handlers for restored messages
            attachScrollArrows(wrapper);

            messagesEl.appendChild(wrapper);
        }
    }

    //  👍/👎 feedback row (CSAT)
    // Rendered under each bot ANSWER. Clicking fires feedback_up/feedback_down
    // through the existing /track pipe, keyed by the message index (data-turn)
    // so a 👎 points at the exact answer to review. The handler is delegated on
    // messagesEl, so it works for both freshly-rendered and reloaded messages.
    var AED_THUMB_UP = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/></svg>';
    var AED_THUMB_DOWN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"/></svg>';

    function renderFeedbackRow(turn) {
        return '<div class="aed-feedback" data-turn="' + escapeAttr(String(turn)) + '">' +
            '<span class="aed-feedback-q">Was this helpful?</span>' +
            '<button type="button" class="aed-fb-btn" data-fb="up" aria-label="Helpful" title="Helpful">' + AED_THUMB_UP + '</button>' +
            '<button type="button" class="aed-fb-btn" data-fb="down" aria-label="Not helpful" title="Not helpful">' + AED_THUMB_DOWN + '</button>' +
            '</div>';
    }

    //  Product cards renderer
    function renderProductCards(cards) {
        if (cards.length <= 1) {
            // Single card — no arrows needed
            var html = '<div class="aed-products">';
            html += renderOneCard(cards[0]);
            html += '</div>';
            return html;
        }
        var html = '<div class="aed-products-wrapper">';
        html += '<div class="aed-products">';
        cards.forEach(function (p) {
            html += renderOneCard(p);
        });
        html += '</div>';
        html += '<div class="aed-scroll-nav">';
        html += '<button class="aed-scroll-btn aed-scroll-left" aria-label="Scroll left">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg>' +
            '</button>';
        html += '<button class="aed-scroll-btn aed-scroll-right" aria-label="Scroll right">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg>' +
            '</button>';
        html += '</div>';
        html += '</div>';
        return html;
    }

    // Attach a disposable attribution ID in utm_content while preserving existing query parameters and fragments.
    function buildTrackedProductUrl(permalink) {
        if (!permalink || permalink === '#') return permalink || '#';
        var params = ['utm_source=chatbot', 'utm_medium=widget', 'utm_campaign=rescue_rita'];
        params.push('utm_content=' + encodeURIComponent(ensureCid()));
        var url = permalink, frag = '';
        var hashIdx = url.indexOf('#');
        if (hashIdx >= 0) { frag = url.slice(hashIdx); url = url.slice(0, hashIdx); }
        var sep = url.indexOf('?') >= 0 ? '&' : '?';
        return url + sep + params.join('&') + frag;
    }

    function renderOneCard(p) {
        var imgSrc = p.thumbnail_url || p.image_url || '';
        var priceHtml = '';

        if (p.on_sale && p.sale_price) {
            priceHtml =
                '<span class="aed-price-original">$' + escapeHtml(formatPrice(p.regular_price)) + '</span>' +
                '<span class="aed-price-current">$' + escapeHtml(formatPrice(p.sale_price)) + '</span>' +
                '<span class="aed-badge-sale">Sale</span>';
        } else {
            priceHtml = '<span class="aed-price-current">$' + escapeHtml(formatPrice(p.price)) + '</span>';
        }

        var reasonHtml = p.recommendation_reason
            ? '<div class="aed-product-reason">' + escapeHtml(p.recommendation_reason) + '</div>'
            : '';

        return '' +
            '<div class="aed-product-card">' +
                (imgSrc ? '<img class="aed-product-img" src="' + escapeAttr(imgSrc) + '" alt="' + escapeAttr(p.name) + '" loading="lazy">' : '') +
                '<div class="aed-product-body">' +
                    '<div class="aed-product-brand">' + escapeHtml(p.brand || '') + '</div>' +
                    '<div class="aed-product-name">' + escapeHtml(p.name) + '</div>' +
                    '<div class="aed-product-price">' + priceHtml + '</div>' +
                    reasonHtml +
                '</div>' +
                '<div class="aed-product-actions">' +
                    '<a class="aed-btn-view" href="' + escapeAttr(buildTrackedProductUrl(p.permalink)) + '" target="_blank" rel="noopener" data-product-id="' + escapeAttr(String(p.product_id == null ? '' : p.product_id)) + '">' +
                        'View Product' +
                        '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg>' +
                    '</a>' +
                '</div>' +
            '</div>';
    }

    //  Upsell section renderer
    function renderUpsellSection(message, cards) {
        var titleText = message || 'You might also like';
        var html = '<div class="aed-upsell">';
        html += '<div class="aed-upsell-title">';
        html += '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>';
        html += escapeHtml(titleText);
        html += '</div>';
        if (cards.length > 1) {
            html += '<div class="aed-products-wrapper">';
            html += '<div class="aed-products">';
            cards.forEach(function (p) { html += renderOneCard(p); });
            html += '</div>';
            html += '<div class="aed-scroll-nav">';
            html += '<button class="aed-scroll-btn aed-scroll-left" aria-label="Scroll left"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg></button>';
            html += '<button class="aed-scroll-btn aed-scroll-right" aria-label="Scroll right"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg></button>';
            html += '</div>';
            html += '</div>';
        } else {
            html += '<div class="aed-products">';
            cards.forEach(function (p) { html += renderOneCard(p); });
            html += '</div>';
        }
        html += '</div>';
        return html;
    }

    //  Typing indicator
    function showTyping(show) {
        if (typingEl) {
            typingEl.hidden = !show;
        }
        if (show) scrollToBottom();
    }

    //  Auto-scroll
    function scrollToBottom() {
        requestAnimationFrame(function () {
            messagesEl.scrollTop = messagesEl.scrollHeight;
        });
    }

    //  Message persistence
    function saveMessage(msg) {
        messages.push(msg);
        // Cap at 50 messages
        if (messages.length > 50) {
            messages = messages.slice(-50);
        }
        try {
            localStorage.setItem('aed_chat_messages', JSON.stringify(messages));
        } catch (e) {
            // Quota exceeded — trim to last 20 messages and retry
            messages = messages.slice(-20);
            try { localStorage.setItem('aed_chat_messages', JSON.stringify(messages)); }
            catch (e2) { localStorage.removeItem('aed_chat_messages'); }
        }
    }

    //  Minimal markdown renderer
    function renderMarkdown(text) {
        if (!text) return '';

        // Escape HTML first
        var html = escapeHtml(text);

        // Bold: **text**
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

        // Links: [text](url) — only allow safe protocols (blocks javascript:, data:, etc.)
        html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (match, text, url) {
            // Undo escapeHtml on URL (it was pre-escaped above) before re-encoding for href
            var rawUrl = url.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"');
            if (!/^(https?:\/\/|mailto:|#|\.?\/)/.test(rawUrl)) return text;
            return '<a href="' + escapeAttr(rawUrl) + '" target="_blank" rel="noopener">' + text + '</a>';
        });

        // Auto-linkify bare URLs not already inside an href attribute
        html = html.replace(/(?<![="'])(https?:\/\/[^\s<>"']+)/g, function (match) {
            var rawUrl = match.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"');
            return '<a href="' + escapeAttr(rawUrl) + '" target="_blank" rel="noopener">' + match + '</a>';
        });

        // Unordered lists: lines starting with - or *
        html = html.replace(/^[\-\*]\s+(.+)$/gm, '<li>$1</li>');
        html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');

        // Ordered lists: lines starting with 1. 2. etc.
        html = html.replace(/^\d+\.\s+(.+)$/gm, '<li>$1</li>');
        // Wrap consecutive <li> not already inside <ul> into <ol>
        html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, function (match, _, offset) {
            // Skip if already wrapped in <ul> (from unordered list above)
            var before = html.substring(0, offset);
            if (before.lastIndexOf('<ul>') > before.lastIndexOf('</ul>')) return match;
            if (match.indexOf('<ul>') !== -1) return match;
            return '<ol>' + match + '</ol>';
        });

        // Newlines to <br> (but not inside lists)
        html = html.replace(/\n/g, '<br>');

        // Clean up: remove <br> right after <ul>/<ol> and before </ul>/</ol>
        html = html.replace(/<br>(<\/?[uo]l>)/g, '$1');
        html = html.replace(/(<\/?[uo]l>)<br>/g, '$1');
        html = html.replace(/<br>(<li>)/g, '$1');
        html = html.replace(/(<\/li>)<br>/g, '$1');

        return html;
    }

    //  Utility functions
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    function escapeAttr(str) {
        return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function formatPrice(val) {
        var num = parseFloat(val);
        if (isNaN(num)) return '0.00';
        return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    } // end init()

    // Run after DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
