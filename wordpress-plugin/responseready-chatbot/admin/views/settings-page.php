<?php
/**
 * Admin settings form template.
 *
 * @var array $opts  Merged options (passed from AEDLC_Admin::render_page).
 * @package ResponseReady_Chatbot
 */

defined('ABSPATH') || exit;

$key = AEDLC_Config::OPTION_KEY;
?>
<div class="wrap">
    <h1>Response Ready Chatbot — Settings</h1>

    <form method="post" action="options.php">
        <?php settings_fields(AEDLC_Config::SETTINGS_GROUP); ?>

        <table class="form-table" role="presentation">

            <tr>
                <th scope="row">
                    <label for="aedlc_backend_url">Backend URL <span style="color:red">*</span></label>
                </th>
                <td>
                    <input type="url" id="aedlc_backend_url"
                           name="<?php echo esc_attr($key); ?>[backend_url]"
                           value="<?php echo esc_attr($opts['backend_url']); ?>"
                           class="regular-text" placeholder="https://responseready-chatbot.mediya.dev" required>
                    <p class="description">
                        URL of the FastAPI chatbot backend, <strong>without</strong> a trailing slash.
                        Must be HTTPS if your site is HTTPS — otherwise the browser will block all chat requests as mixed content.
                    </p>
                </td>
            </tr>

            <tr>
                <th scope="row">
                    <label for="aedlc_primary_color">Primary color</label>
                </th>
                <td>
                    <input type="color" id="aedlc_primary_color"
                           name="<?php echo esc_attr($key); ?>[primary_color]"
                           value="<?php echo esc_attr($opts['primary_color']); ?>">
                    <code style="margin-left:8px"><?php echo esc_html($opts['primary_color']); ?></code>
                    <p class="description">Used for the toggle button, user message bubbles, and links.</p>
                </td>
            </tr>

            <tr>
                <th scope="row">
                    <label for="aedlc_accent_color">Accent color</label>
                </th>
                <td>
                    <input type="color" id="aedlc_accent_color"
                           name="<?php echo esc_attr($key); ?>[accent_color]"
                           value="<?php echo esc_attr($opts['accent_color']); ?>">
                    <code style="margin-left:8px"><?php echo esc_html($opts['accent_color']); ?></code>
                    <p class="description">Used for the online status dot and CTA buttons.</p>
                </td>
            </tr>

            <tr>
                <th scope="row">Position</th>
                <td>
                    <label>
                        <input type="radio"
                               name="<?php echo esc_attr($key); ?>[position]"
                               value="right" <?php checked($opts['position'], 'right'); ?>>
                        Bottom right
                    </label>&nbsp;&nbsp;
                    <label>
                        <input type="radio"
                               name="<?php echo esc_attr($key); ?>[position]"
                               value="left" <?php checked($opts['position'], 'left'); ?>>
                        Bottom left
                    </label>
                </td>
            </tr>

            <tr>
                <th scope="row">
                    <label for="aedlc_z_index">Z-index</label>
                </th>
                <td>
                    <input type="number" id="aedlc_z_index"
                           name="<?php echo esc_attr($key); ?>[z_index]"
                           value="<?php echo esc_attr($opts['z_index']); ?>" class="small-text"
                           min="1" max="2147483647">
                    <p class="description">Increase if the widget falls behind other elements (default: 9999).</p>
                </td>
            </tr>

            <tr>
                <th scope="row">
                    <label for="aedlc_greeting">Greeting</label>
                </th>
                <td>
                    <textarea id="aedlc_greeting"
                              name="<?php echo esc_attr($key); ?>[greeting]"
                              rows="4" class="large-text"><?php echo esc_textarea($opts['greeting']); ?></textarea>
                    <p class="description">First bot message shown when the chat panel is opened.</p>
                </td>
            </tr>

            <tr>
                <th scope="row">
                    <label for="aedlc_beta_notice">Beta notice</label>
                </th>
                <td>
                    <input type="text" id="aedlc_beta_notice"
                           name="<?php echo esc_attr($key); ?>[beta_notice]"
                           value="<?php echo esc_attr($opts['beta_notice']); ?>"
                           class="regular-text" placeholder="Beta — answers may vary">
                    <p class="description">Status text shown next to the bot name in the header. Leave blank to hide the row entirely.</p>
                </td>
            </tr>

            <tr>
                <th scope="row">Teaser bubble</th>
                <td>
                    <label>
                        <input type="checkbox" id="aedlc_show_teaser"
                               name="<?php echo esc_attr($key); ?>[show_teaser]"
                               value="1" <?php checked(! empty($opts['show_teaser'])); ?>>
                        Show a teaser bubble above the chat button a few seconds after page load
                    </label>
                    <p class="description">
                        A small nudge inviting visitors to chat — shows on all pages and does <strong>not</strong> auto-open the full panel. Once a visitor dismisses it (the X), it stays hidden for them. Default on.
                    </p>
                </td>
            </tr>

            <tr>
                <th scope="row">
                    <label for="aedlc_teaser_text">Teaser text</label>
                </th>
                <td>
                    <input type="text" id="aedlc_teaser_text"
                           name="<?php echo esc_attr($key); ?>[teaser_text]"
                           value="<?php echo esc_attr($opts['teaser_text']); ?>"
                           class="regular-text" placeholder="Have questions about AEDs? Ask me here!">
                    <p class="description">Message shown inside the teaser bubble.</p>
                </td>
            </tr>

            <tr>
                <th scope="row">
                    <label for="aedlc_show_on_pages">Show on</label>
                </th>
                <td>
                    <input type="text" id="aedlc_show_on_pages"
                           name="<?php echo esc_attr($key); ?>[show_on_pages]"
                           value="<?php echo esc_attr($opts['show_on_pages']); ?>"
                           class="regular-text" placeholder="all">
                    <p class="description">
                        <code>all</code> = every page &nbsp;|&nbsp;
                        <code>home</code> = front page only &nbsp;|&nbsp;
                        Comma-separated page IDs: <code>12,34,56</code>
                    </p>
                </td>
            </tr>

        </table>

        <?php submit_button('Save settings'); ?>
    </form>

    <?php if (empty($opts['backend_url'])) : ?>
        <div class="notice notice-warning inline">
            <p><strong>Heads up:</strong> set the Backend URL to activate the chatbot. The widget will not load until this is configured.</p>
        </div>
    <?php endif; ?>

    <h2 style="margin-top:32px">Quick-start chips</h2>
    <p>The chips shown above the message input are hardcoded in <code>includes/class-aedlc-config.php</code> (<code>AEDLC_Config::chips()</code>). Edit that file to change them.</p>
</div>
