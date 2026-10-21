<?php
/**
 * Chat widget HTML shell.
 *
 * Lifted verbatim from frontend/index.html (#aed-chatbot-root block).
 * Keep DOM IDs/classes in sync with assets/js/chatbot.js — they are queried by ID.
 *
 * @package CPR1_Chatbot
 */

defined('ABSPATH') || exit;
?>
<div id="aed-chatbot-root" aria-label="CPR1 AI Chatbot" role="complementary">

    <button id="aed-chat-toggle" aria-label="Open chatbot" aria-expanded="false">
        <span class="aed-icon-chat" aria-hidden="true">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
                <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z"/>
                <path d="M7 9h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z"/>
            </svg>
        </span>
        <span class="aed-icon-close" aria-hidden="true" style="display:none">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
            </svg>
        </span>
        <span class="aed-btn-label">Chat with us</span>
    </button>

    <?php /* Proactive teaser bubble — small nudge above the toggle. Text is
        set from window.AEDChatbotConfig.teaserText by chatbot.js. Ships hidden. */ ?>
    <div id="aed-teaser" class="aed-teaser" hidden>
        <button class="aed-teaser-dismiss" aria-label="Dismiss">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
            </svg>
        </button>
        <div class="aed-teaser-body" role="button" tabindex="0" aria-label="Open chat">
            <div class="aed-teaser-avatar" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/>
                </svg>
            </div>
            <div class="aed-teaser-text">Questions about CPR training or AEDs? Ask me here!</div>
        </div>
    </div>

    <div id="aed-chat-panel" role="dialog" aria-modal="true" aria-label="CPR1 Chatbot" hidden>

        <div class="aed-header">
            <div class="aed-header-info">
                <div class="aed-header-avatar" aria-hidden="true">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/>
                    </svg>
                </div>
                <div>
                    <div class="aed-header-name">Coach Casey</div>
                    <div class="aed-header-status"><span class="aed-status-dot"></span> <span id="aed-header-status-text">Beta — answers may vary</span></div>
                </div>
            </div>
            <div class="aed-header-actions">
                <button id="aed-reset" class="aed-reset-btn" aria-label="Start over" title="Start over">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                        <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
                        <path d="M3 3v5h5"/>
                    </svg>
                </button>
                <button id="aed-close" class="aed-header-close" aria-label="Close chatbot">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
                    </svg>
                </button>
            </div>
        </div>

        <div id="aed-messages" class="aed-messages" role="log" aria-live="polite" aria-label="Chat messages"></div>

        <div id="aed-typing" class="aed-typing" hidden aria-label="Coach Casey is typing...">
            <div class="aed-typing-avatar">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/>
                </svg>
            </div>
            <div class="aed-typing-dots" aria-hidden="true">
                <span></span><span></span><span></span>
            </div>
        </div>

        <div id="aed-chips" class="aed-chips"></div>

        <div class="aed-input-area">
            <textarea id="aed-input" class="aed-input"
                      placeholder="Ask about training courses, AEDs, or pricing..."
                      rows="1"
                      aria-label="Type a message"
                      maxlength="2000"></textarea>
            <button id="aed-send" class="aed-send-btn" aria-label="Send message">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
                </svg>
            </button>
        </div>

        <div class="aed-footer">
            <span>Powered by <strong>CPR1</strong> &middot; Press Enter to send</span>
        </div>

        <?php /*
            Debug toggle button + #aed-debug-panel are intentionally omitted.
            chatbot.js gates both its click handler and the Ctrl+D shortcut on
            #aed-debug-toggle being present, so removing the DOM here keeps
            customers from hijacking Ctrl+D or seeing pipeline internals.
            Use frontend/index.html (the dev test page) to debug.
        */ ?>

    </div>
</div>
