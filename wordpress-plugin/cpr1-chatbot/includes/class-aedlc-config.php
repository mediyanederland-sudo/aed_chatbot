<?php
/**
 * Central configuration: defaults, sanitization, visibility, chip definitions.
 *
 * @package CPR1_Chatbot
 */

defined('ABSPATH') || exit;

class AEDLC_Config
{
    const OPTION_KEY     = 'aedlc_chatbot_options';
    const SETTINGS_GROUP = 'aedlc_chatbot_group';

    /**
     * Default option values.
     */
    public static function defaults(): array
    {
        return [
            // Tenant slug sent to the multisite backend (resolves catalog,
            // prompt, phone). Baked per-site by wordpress-plugin/build_site_zips.py.
            'site'          => 'cpr1',
            'backend_url'   => 'https://aedleader-chatbot.mediya.dev',
            'primary_color' => '#0B5ED7',
            'accent_color'  => '#198754',
            'position'      => 'right',
            'z_index'       => '9999',
            'greeting'      => "Hi, I'm Coach Casey — your CPR1 assistant. I can help you book CPR, AED & First Aid training for your team, find the right AED, and answer your questions.\n\nAre you interested in training or AED equipment today? And in case we get disconnected, feel free to share your name and email here.",
            'beta_notice'   => 'Beta — answers may vary',
            'show_on_pages' => 'all',
            'show_teaser'   => true,
            'teaser_text'   => 'Questions about CPR training or AEDs? Ask me here!',
        ];
    }

    /**
     * Quick-start chips. Hardcoded for v1 — chip payload shape varies by action,
     * so a settings-page repeater is too much UI for v1. Edit this array to change them.
     *
     * Schema: { id, label, action: 'prompt'|'url'|'info', payload }
     *   prompt → payload = string
     *   url    → payload = [ 'url' => string, 'target' => '_blank' ]
     *   info   → payload = [ 'title' => string, 'lines' => string[], 'cta' => [ 'label' => string, 'url' => string ] ]
     */
    public static function chips(): array
    {
        return [
            [
                'id'      => 'find_course',
                'label'   => 'CPR/First-Aid training',
                'action'  => 'prompt',
                'payload' => 'Help me find the right CPR/First-Aid training course for my team',
            ],
            [
                'id'      => 'find_aed',
                'label'   => 'AED equipment',
                'action'  => 'prompt',
                'payload' => 'Help me find the right AED for my needs',
            ],
            [
                'id'      => 'inquiry',
                'label'   => 'Submit an inquiry',
                'action'  => 'info',
                'payload' => [
                    'title' => 'Submit an Inquiry',
                    'lines' => [
                        'Need bulk pricing, a compliance program, or a custom package?',
                        'Send us the details and our team will respond within 1 business day.',
                    ],
                    'cta'   => [
                        'label' => 'Open inquiry form',
                        'url'   => 'https://cpr1.com/contact/',
                    ],
                ],
            ],




            [
                'id'      => 'support',
                'label'   => 'Contact support',
                'action'  => 'info',
                'payload' => [
                    'title' => 'Contact CPR1',
                    'lines' => [
                        '📞 858-665-8444',
                        '✉️ info@cpr1.com',
                        'Hours: Mon to Fri, 8:00 AM to 4:30 PM PST',
                    ],
                ],
            ],
            [
                'id'      => 'faq',
                'label'   => 'FAQ',
                'action'  => 'prompt',
                'payload' => 'What do most customers ask before booking training or buying an AED?',
            ],
        ];
    }

    /**
     * Merged (defaults + saved) options.
     */
    public static function get_options(): array
    {
        return array_merge(
            self::defaults(),
            (array) get_option(self::OPTION_KEY, [])
        );
    }

    /**
     * Sanitize incoming option values from the settings form.
     */
    public static function sanitize($input): array
    {
        $input    = is_array($input) ? $input : [];
        $defaults = self::defaults();
        $clean    = [];

        $clean['site']          = sanitize_key($input['site'] ?? $defaults['site']) ?: $defaults['site'];
        $clean['backend_url']   = esc_url_raw(rtrim((string) ($input['backend_url'] ?? ''), '/'));
        $clean['primary_color'] = sanitize_hex_color($input['primary_color'] ?? '') ?: $defaults['primary_color'];
        $clean['accent_color']  = sanitize_hex_color($input['accent_color']  ?? '') ?: $defaults['accent_color'];
        $clean['position']      = in_array($input['position'] ?? '', ['right', 'left'], true) ? $input['position'] : 'right';
        $clean['z_index']       = absint($input['z_index'] ?? 9999) ?: 9999;
        $clean['greeting']      = sanitize_textarea_field($input['greeting']    ?? $defaults['greeting']);
        $clean['beta_notice']   = sanitize_text_field($input['beta_notice']     ?? '');
        $clean['show_on_pages'] = sanitize_text_field($input['show_on_pages']   ?? 'all');
        $clean['show_teaser']   = ! empty($input['show_teaser']);
        $clean['teaser_text']   = sanitize_text_field($input['teaser_text'] ?? '') ?: $defaults['teaser_text'];

        return $clean;
    }

    /**
     * Should the widget render on the current request?
     */
    public static function should_show(): bool
    {
        if (is_admin() || wp_doing_ajax() || wp_doing_cron() || (defined('REST_REQUEST') && REST_REQUEST)) {
            return false;
        }

        $opts = self::get_options();
        $rule = trim((string) $opts['show_on_pages']);

        if ($rule === '' || $rule === 'all') {
            return true;
        }

        if ($rule === 'home') {
            return is_front_page() || is_home();
        }

        $ids = array_filter(array_map('intval', explode(',', $rule)));
        return in_array(get_queried_object_id(), $ids, true);
    }
}
