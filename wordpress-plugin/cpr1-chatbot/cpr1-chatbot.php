<?php
/**
 * Plugin Name:       CPR1 Chatbot
 * Plugin URI:        https://cpr1.com
 * Description:       Floating AI chatbot widget for the CPR1 WooCommerce store. Connects to the CPR1 FastAPI RAG backend.
 * Version:           1.1.0
 * Requires at least: 5.8
 * Requires PHP:      7.4
 * Author:            Mediya
 * Author URI:        https://mediya.nl
 * License:           GPL-2.0-or-later
 * License URI:       https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain:       cpr1-chatbot
 */

defined('ABSPATH') || exit;

define('AEDLC_VERSION', '1.1.0');
define('AEDLC_DIR',     plugin_dir_path(__FILE__));
define('AEDLC_URL',     plugin_dir_url(__FILE__));
define('AEDLC_FILE',    __FILE__);

/**
 * Per-asset cache-buster. Returns the file's mtime so any change to the CSS
 * or JS auto-busts the browser cache without manual version bumps. Falls
 * back to AEDLC_VERSION if the file is missing.
 *
 * @param string $relative_path  Path inside the plugin (e.g. 'assets/css/chatbot.css').
 * @return string                Version string for wp_enqueue_*.
 */
function aedlc_asset_version(string $relative_path): string
{
    $abs = AEDLC_DIR . $relative_path;
    return file_exists($abs) ? (string) filemtime($abs) : AEDLC_VERSION;
}

require_once AEDLC_DIR . 'includes/class-aedlc-config.php';

add_action('plugins_loaded', function () {
    $db_version = get_option('aedlc_db_version', '0');
    if (version_compare($db_version, AEDLC_VERSION, '<')) {
        $saved    = (array) get_option(AEDLC_Config::OPTION_KEY, []);
        $defaults = AEDLC_Config::defaults();
        $saved['greeting']    = $defaults['greeting'];
        $saved['beta_notice'] = $defaults['beta_notice'];
        update_option(AEDLC_Config::OPTION_KEY, $saved);
        update_option('aedlc_db_version', AEDLC_VERSION);
    }
});

if (is_admin()) {
    require_once AEDLC_DIR . 'admin/class-aedlc-admin.php';
    new AEDLC_Admin();
}

require_once AEDLC_DIR . 'includes/class-aedlc-frontend.php';
new AEDLC_Frontend();
