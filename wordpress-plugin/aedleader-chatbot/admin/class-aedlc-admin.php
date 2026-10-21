<?php
/**
 * Admin settings page (Settings → AED Chatbot).
 *
 * @package AEDLeader_Chatbot
 */

defined('ABSPATH') || exit;

class AEDLC_Admin
{
    public function __construct()
    {
        add_action('admin_menu', [$this, 'add_menu_page']);
        add_action('admin_init', [$this, 'register_settings']);
    }

    public function add_menu_page(): void
    {
        add_options_page(
            'AED Leader Chatbot',
            'AED Chatbot',
            'manage_options',
            'aedleader-chatbot',
            [$this, 'render_page']
        );
    }

    public function register_settings(): void
    {
        register_setting(
            AEDLC_Config::SETTINGS_GROUP,
            AEDLC_Config::OPTION_KEY,
            [
                'type'              => 'array',
                'sanitize_callback' => [AEDLC_Config::class, 'sanitize'],
                'default'           => AEDLC_Config::defaults(),
            ]
        );
    }

    public function render_page(): void
    {
        if (! current_user_can('manage_options')) {
            return;
        }

        $opts = AEDLC_Config::get_options();
        include AEDLC_DIR . 'admin/views/settings-page.php';
    }
}
