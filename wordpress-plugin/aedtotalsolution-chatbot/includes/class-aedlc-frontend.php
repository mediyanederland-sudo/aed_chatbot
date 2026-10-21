<?php
/**
 * Frontend: enqueue assets and render the floating chat widget shell.
 *
 * @package AEDTotalSolution_Chatbot
 */

defined('ABSPATH') || exit;

class AEDLC_Frontend
{
    public function __construct()
    {
        add_action('wp_enqueue_scripts', [$this, 'enqueue_assets']);
        add_action('wp_footer',          [$this, 'render_widget']);
    }

    public function enqueue_assets(): void
    {
        if (! AEDLC_Config::should_show()) {
            return;
        }

        $opts = AEDLC_Config::get_options();
        if (empty($opts['backend_url'])) {
            return;
        }

        wp_enqueue_style(
            'aedlc-chatbot',
            AEDLC_URL . 'assets/css/chatbot.css',
            [],
            aedlc_asset_version('assets/css/chatbot.css')
        );

        wp_enqueue_script(
            'aedlc-chatbot',
            AEDLC_URL . 'assets/js/chatbot.js',
            [],
            aedlc_asset_version('assets/js/chatbot.js'),
            true
        );

        $beta_notice = (string) $opts['beta_notice'];
        // Empty → JS reads `false` and hides the entire status row.
        $beta_for_js = ($beta_notice === '') ? false : $beta_notice;

        $config = [
            'site'         => (string) ($opts['site'] ?? 'aedtotalsolution'),
            'backendUrl'   => esc_url_raw(rtrim((string) $opts['backend_url'], '/')),
            'primaryColor' => $opts['primary_color'],
            'accentColor'  => $opts['accent_color'],
            'position'     => $opts['position'],
            'zIndex'       => (int) $opts['z_index'],
            'greeting'     => $opts['greeting'],
            'betaNotice'   => $beta_for_js,


            'showTeaser'   => (bool) ($opts['show_teaser'] ?? false),
            'teaserText'   => (string) ($opts['teaser_text'] ?? ''),
            'chips'        => AEDLC_Config::chips(),
        ];

        // wp_add_inline_script preserves types (booleans, ints, nested arrays) —
        // wp_localize_script would string-cast every scalar.
        wp_add_inline_script(
            'aedlc-chatbot',
            'window.AEDChatbotConfig = ' . wp_json_encode($config) . ';',
            'before'
        );
    }

    public function render_widget(): void
    {
        if (! AEDLC_Config::should_show()) {
            return;
        }
        $opts = AEDLC_Config::get_options();
        if (empty($opts['backend_url'])) {
            return;
        }

        include AEDLC_DIR . 'templates/widget.php';
    }
}
