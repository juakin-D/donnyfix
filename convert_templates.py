"""
Convert all standalone HTML templates to use Jinja2 template inheritance.
Customer templates extend base.html; admin templates extend admin_base.html.
Run once: python convert_templates.py
"""
import re, os, sys

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')

# ── Customer templates that extend base.html ───────────────────────────────
CUSTOMER_TEMPLATES = [
    '404.html', '500.html', 'account_edit.html', 'booking.html',
    'confirmation.html', 'customer_login.html', 'dashboard.html',
    'forgot_password.html', 'index.html', 'installment_apply.html',
    'installment_detail.html', 'privacy.html', 'register.html',
    'reset_password.html', 'shop.html', 'shop_detail.html',
    'shop_reservations.html',
]

# ── Admin templates that extend admin_base.html ───────────────────────────
ADMIN_TEMPLATES = [
    'admin.html', 'admin_installments.html', 'admin_inventory.html',
    'admin_members.html', 'admin_revenue.html', 'admin_shop.html',
    'admin_staff.html',
]

# ── Per-template block hints ───────────────────────────────────────────────
TITLES = {
    '404.html':               'Page Not Found — DonnyPhonehub Gh',
    '500.html':               'Server Error — DonnyPhonehub Gh',
    'account_edit.html':      'Edit Profile — DonnyPhonehub Gh',
    'booking.html':           'Book a Repair — DonnyPhonehub Gh',
    'confirmation.html':      'Booking Confirmed — DonnyPhonehub Gh',
    'customer_login.html':    'Log In — DonnyPhonehub Gh',
    'dashboard.html':         'My Dashboard — DonnyPhonehub Gh',
    'forgot_password.html':   'Forgot Password — DonnyPhonehub Gh',
    'index.html':             'DonnyPhonehub Gh — Phone Sales, Repairs & Membership | Accra',
    'installment_apply.html': 'Apply for Installment — DonnyPhonehub Gh',
    'installment_detail.html':'Installment Plan — DonnyPhonehub Gh',
    'privacy.html':           'Privacy Policy — DonnyPhonehub Gh',
    'register.html':          'Join Free — DonnyPhonehub Gh',
    'reset_password.html':    'Reset Password — DonnyPhonehub Gh',
    'shop.html':              'Buy a Phone — DonnyPhonehub Gh',
    'shop_detail.html':       'Phone Details — DonnyPhonehub Gh',
    'shop_reservations.html': 'My Reservations — DonnyPhonehub Gh',
    'admin.html':             'Bookings — Admin',
    'admin_installments.html':'Installments — Admin',
    'admin_inventory.html':   'Inventory — Admin',
    'admin_members.html':     'Members — Admin',
    'admin_revenue.html':     'Revenue — Admin',
    'admin_shop.html':        'Shop Orders — Admin',
    'admin_staff.html':       'Staff — Admin',
}

# Admin topbar titles/subtitles (for admin_base blocks)
ADMIN_TOPBAR = {
    'admin.html':             ('Bookings',     'All repair & service requests'),
    'admin_installments.html':('Installments', 'Manage payment plans'),
    'admin_inventory.html':   ('Inventory',    'Device stock management'),
    'admin_members.html':     ('Members',      'Customer membership records'),
    'admin_revenue.html':     ('Revenue',      'Financial overview & analytics'),
    'admin_shop.html':        ('Shop Orders',  'Reservations & enquiries'),
    'admin_staff.html':       ('Staff',        'Team management'),
}

# Pages that have a footer block (keep footer in page template)
HAS_FOOTER = {'index.html', 'confirmation.html', 'privacy.html',
              'shop.html', 'shop_detail.html', 'shop_reservations.html'}

# ── Patterns to strip from customer templates ──────────────────────────────
# These appear in every template and are now provided by base.html

# Start of WA float (first shared HTML after <body>)
WA_START_PATTERNS = [
    r'<!--\s*WhatsApp\s*(Floating Button|Float)\s*-->',
    r'<a href="https://wa\.me/',
]

# End of desktop nav — varies slightly
DESKTOP_NAV_END = r'</nav>'

# Start of bottom nav
BOTTOM_NAV_START = r'<nav class="bottom-nav"'

# The nav open/close JS block
NAV_JS_PATTERN = r'function openMenu\(\)\{.*?closeMenu\(\)\}\)'

# Flash messages block inside template content
FLASH_BLOCK_PATTERN = (
    r'\{%\s*with messages\s*=\s*get_flashed_messages\(with_categories'
    r'.*?'
    r'\{%\s*endwith\s*%\}'
)

# Common CSS classes to detect the end of page-specific CSS and start of shared nav CSS
NAV_CSS_START_MARKERS = [
    '.wa-float{',
    '.wa-float {',
    '.mobile-topbar{',
    '.mobile-topbar {',
]


def read(fname):
    path = os.path.join(TEMPLATES_DIR, fname)
    with open(path, encoding='utf-8') as f:
        return f.read()


def write(fname, content):
    path = os.path.join(TEMPLATES_DIR, fname)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def extract_style_block(content):
    """Return (css_text, content_without_style_block)."""
    m = re.search(r'<style>(.*?)</style>', content, re.DOTALL)
    if not m:
        return '', content
    css = m.group(1)
    rest = content[:m.start()] + content[m.end():]
    return css, rest


def strip_common_nav_css(css):
    """Remove the mobile nav / desktop nav CSS that lives in base.html."""
    markers = [
        '.wa-float{', '.wa-float {',
        '.mobile-topbar{', '.mobile-topbar {',
        '.desktop-nav{', '.desktop-nav {',
        '.bottom-nav{', '.bottom-nav {',
    ]
    for marker in markers:
        idx = css.find(marker)
        if idx != -1:
            css = css[:idx].rstrip()
            break
    return css.strip()


def find_matching_div_close(content, open_tag_pos):
    """Return index of the </div> that closes the <div> at open_tag_pos."""
    depth = 1
    i = content.find('>', open_tag_pos) + 1  # skip past the opening tag
    while i < len(content) and depth > 0:
        open_div  = content.find('<div', i)
        close_div = content.find('</div>', i)
        if close_div == -1:
            break
        if open_div != -1 and open_div < close_div:
            char_after = content[open_div + 4:open_div + 5]
            if char_after in (' ', '>', '\t', '\n'):
                depth += 1
            i = open_div + 4
        else:
            depth -= 1
            if depth == 0:
                return close_div
            i = close_div + 6
    return -1


def find_content_start(content):
    """Return index right after the nav system ends.

    For templates with a desktop nav: right after its closing </nav>.
    For templates without a desktop nav: right after the openMenu </script>.
    """
    nav_start = content.find('<nav class="desktop-nav"')
    if nav_start != -1:
        # Depth-match the desktop nav's closing </nav>
        depth = 0
        i = nav_start
        while i < len(content):
            if content[i:].startswith('<nav') and not content[i:].startswith('</nav'):
                depth += 1
                i += 4
            elif content[i:].startswith('</nav>'):
                depth -= 1
                if depth == 0:
                    return i + len('</nav>')
                i += 6
            else:
                i += 1
        return len(content)

    # No desktop nav — find the openMenu script and return after its </script>
    om_idx = content.find('openMenu')
    if om_idx != -1:
        script_close = content.find('</script>', om_idx)
        if script_close != -1:
            return script_close + len('</script>')

    # Last resort: right after <body>
    body_idx = content.find('<body>')
    return body_idx + len('<body>') if body_idx != -1 else 0


def find_content_end(content):
    """Return index of </body> — page content always ends here."""
    idx = content.rfind('</body>')
    return idx if idx != -1 else len(content)


def find_footer(content, start, end):
    """Return (footer_html, new_end) if there's a <footer> block."""
    chunk = content[start:end]
    footer_match = re.search(r'<footer\b.*?</footer>', chunk, re.DOTALL)
    if not footer_match:
        return None, end
    footer_html = footer_match.group(0)
    # Adjust end to exclude footer from main content
    footer_start_abs = start + footer_match.start()
    return footer_html, footer_start_abs


def strip_flash_from_content(content):
    """Remove {% with messages = get_flashed_messages... %}...{% endwith %} blocks."""
    return re.sub(FLASH_BLOCK_PATTERN, '', content, flags=re.DOTALL).strip()


def convert_customer(fname):
    content = read(fname)
    title = TITLES.get(fname, 'DonnyPhonehub Gh')

    # ── 1. Extract <style> block ──────────────────────────────────────────
    style_css, content_no_style = extract_style_block(content)
    style_css = strip_common_nav_css(style_css)

    # ── 2. Find page content boundaries ──────────────────────────────────
    body_pos = content.find('<body>')
    wa_pos   = content.find('<a href="https://wa.me/', body_pos) if body_pos != -1 else -1
    wa_gap   = (wa_pos - body_pos) if (wa_pos != -1 and body_pos != -1) else -1

    # Templates where the mobile nav appears BEFORE the page content put the WA
    # float immediately after <body> (gap < 200 chars).  The rare "content-first"
    # template (e.g. register.html) puts the page form first, nav later.
    content_first = wa_gap == -1 or wa_gap > 200

    if content_first:
        # Content is between <body> and the inline <style> block that holds nav CSS
        content_start = body_pos + len('<body>') if body_pos != -1 else 0
        style_in_body = content.find('<style>', content_start)
        content_end   = style_in_body if style_in_body != -1 else content.rfind('</body>')
    else:
        content_start = find_content_start(content)
        content_end   = find_content_end(content)

    # ── 3. Extract footer if present ──────────────────────────────────────
    footer_html = None
    if fname in HAS_FOOTER:
        footer_html, content_end = find_footer(content, content_start, content_end)

    # ── 4. Extract page-specific HTML content ─────────────────────────────
    page_html = content[content_start:content_end].strip()

    # Remove flash messages from page content (base.html handles them)
    page_html = strip_flash_from_content(page_html)

    # Remove any bottom-nav that leaked into the content region
    # (some templates, e.g. account_edit.html, have it after the desktop nav)
    page_html = re.sub(
        r'<nav class="bottom-nav"[^>]*>.*?</nav>', '', page_html, flags=re.DOTALL
    ).strip()

    # ── 5. Check for OG tags ──────────────────────────────────────────────
    og_match = re.search(r'(<meta property="og:.*?(?=<link|<style|<script|</head>))', content, re.DOTALL)
    og_block = og_match.group(1).strip() if og_match else ''

    # ── 6. Check for meta description ─────────────────────────────────────
    desc_match = re.search(r'<meta name="description"[^>]+>', content)
    desc_block = desc_match.group(0) if desc_match else ''

    # ── 7. Lift page-specific <script> blocks out of page_html ───────────
    # base.html already provides openMenu/closeMenu, so skip those.
    # Also pick up scripts that appear AFTER the footer (some templates put
    # their JS block after the <footer> element, before </body>).
    page_scripts_parts = []

    def _collect_script(m):
        sb = m.group(0)
        if 'openMenu' in sb or 'closeMenu' in sb:
            return ''  # nav JS lives in base.html — drop it
        page_scripts_parts.append(sb)
        return ''  # remove from page_html; will appear in page_scripts block

    page_html = re.sub(r'<script\b[^>]*>.*?</script>', _collect_script,
                       page_html, flags=re.DOTALL).strip()

    # Capture scripts between footer end and </body> (missed by page_html scan)
    if footer_html:
        body_end_idx = content.rfind('</body>')
        footer_end_pos = content_end + len(footer_html)
        after_footer = content[footer_end_pos:body_end_idx] if body_end_idx != -1 else ''
        for sm in re.finditer(r'<script\b[^>]*>.*?</script>', after_footer, re.DOTALL):
            sb = sm.group(0)
            if 'openMenu' not in sb and 'closeMenu' not in sb:
                page_scripts_parts.append(sb)

    page_scripts = '\n'.join(page_scripts_parts)

    # ── 8. Assemble new template ──────────────────────────────────────────
    parts = [f'{{% extends "base.html" %}}']

    # Title (only override if not matching default)
    parts.append(f'{{% block title %}}{title}{{% endblock %}}')

    # Meta description
    if desc_block and fname == 'index.html':
        parts.append(f'{{% block meta_description %}}{desc_block}{{% endblock %}}')

    # OG tags
    if og_block and fname == 'index.html':
        parts.append(f'{{% block og_tags %}}{og_block}{{% endblock %}}')

    # Page-specific CSS
    if style_css.strip():
        parts.append(f'{{% block head_extra %}}\n<style>\n{style_css}\n</style>\n{{% endblock %}}')

    # Desktop nav override for index (uses anchor links)
    if fname == 'index.html':
        parts.append(
            '{% block desktop_nav_links %}\n'
            '<a href="#services">Services</a>\n'
            '<a href="#membership">Membership</a>\n'
            '<a href="{{ url_for(\'shop\') }}">Shop</a>\n'
            '<a href="{{ url_for(\'booking\') }}">Book Repair</a>\n'
            '{% if session.get(\'customer_id\') %}\n'
            '  <a href="{{ url_for(\'dashboard\') }}">My Account</a>\n'
            '{% else %}\n'
            '  <a href="{{ url_for(\'customer_login\') }}">Login</a>\n'
            '  <a href="{{ url_for(\'register\') }}" class="nav-cta">Join Free</a>\n'
            '{% endif %}\n'
            '{% endblock %}'
        )

    # Page content
    if page_html:
        parts.append(f'{{% block page_content %}}\n{page_html}\n{{% endblock %}}')

    # Footer block
    if footer_html:
        parts.append(f'{{% block footer %}}\n{footer_html}\n{{% endblock %}}')

    # Page scripts
    if page_scripts.strip():
        parts.append(f'{{% block page_scripts %}}\n  {page_scripts}\n{{% endblock %}}')

    result = '\n\n'.join(parts) + '\n'
    write(fname, result)
    print(f'  OK {fname}')


def convert_admin(fname):
    content = read(fname)
    title = TITLES.get(fname, 'Admin — DonnyPhonehub Gh')
    topbar_title, topbar_sub = ADMIN_TOPBAR.get(fname, ('Admin', ''))

    # ── 1. Page-specific CSS (second <style> block onward) ───────────────
    all_styles = re.findall(r'<style>(.*?)</style>', content, re.DOTALL)
    extra_css = '\n'.join(all_styles[1:]).strip() if len(all_styles) > 1 else ''

    # ── 2. Find page content via bracket-matching on <div class="content"> ─
    content_open = content.find('<div class="content">')
    if content_open == -1:
        content_open = content.find('<div class="content" ')
    content_body_start = content_open + len('<div class="content">') if content_open != -1 else 0

    content_close = find_matching_div_close(content, content_open) if content_open != -1 else -1

    raw_content = content[content_body_start:content_close].strip() if content_close != -1 else ''
    raw_content = strip_flash_from_content(raw_content).strip()

    # ── 3. Page-specific scripts (after the content div, before </body>) ──
    after_content = content[content_close:content.rfind('</body>')] if content_close != -1 else ''
    all_body_scripts = re.findall(r'<script[^>]*>.*?</script>', after_content, re.DOTALL)
    page_scripts_parts = [s for s in all_body_scripts
                          if 'updateTime' not in s and 'live-time' not in s]

    # Extra <style> blocks after content (e.g. modal animations)
    body_styles_extra = re.findall(r'<style>(.*?)</style>', after_content, re.DOTALL)
    modal_style = '\n'.join(body_styles_extra).strip()

    # ── 4. Assemble ───────────────────────────────────────────────────────
    parts = [f'{{% extends "admin_base.html" %}}']
    parts.append(f'{{% block title %}}{title}{{% endblock %}}')
    parts.append(f'{{% block topbar_title %}}{topbar_title}{{% endblock %}}')
    if topbar_sub:
        parts.append(f'{{% block topbar_subtitle %}}{topbar_sub}{{% endblock %}}')

    head_extra_parts = []
    if extra_css.strip():
        head_extra_parts.append(f'<style>\n{extra_css}\n</style>')
    if modal_style.strip():
        head_extra_parts.append(f'<style>\n{modal_style}\n</style>')
    if head_extra_parts:
        parts.append('{{% block head_extra %}}\n{}\n{{% endblock %}}'.format('\n'.join(head_extra_parts)))

    # topbar_right: extra buttons/controls beyond what admin_base provides
    topbar_right_match = re.search(
        r'<div class="topbar-right">(.*?)</div>\s*</div>\s*<!-- Mobile Admin Nav',
        content, re.DOTALL
    )
    if topbar_right_match:
        tr = topbar_right_match.group(1).strip()
        tr = re.sub(r'<div class="topbar-time"[^>]*>.*?</div>', '', tr, flags=re.DOTALL)
        tr = re.sub(r'<div class="status-pill">.*?</div>', '', tr, flags=re.DOTALL)
        tr = re.sub(r'<div style="display:flex.*?</div>', '', tr, flags=re.DOTALL)
        tr = tr.strip()
        if tr:
            parts.append(f'{{% block topbar_right %}}\n{tr}\n{{% endblock %}}')

    if raw_content:
        parts.append(f'{{% block content %}}\n{raw_content}\n{{% endblock %}}')

    if page_scripts_parts:
        parts.append('{{% block scripts %}}\n{}\n{{% endblock %}}'.format(
            '\n'.join(page_scripts_parts)))

    result = '\n\n'.join(parts) + '\n'
    write(fname, result)
    print(f'  OK {fname}')


def main():
    print('Converting customer templates...')
    for fname in CUSTOMER_TEMPLATES:
        try:
            convert_customer(fname)
        except Exception as e:
            print(f'  FAIL {fname}: {e}')
            import traceback; traceback.print_exc()

    print('\nConverting admin templates...')
    for fname in ADMIN_TEMPLATES:
        try:
            convert_admin(fname)
        except Exception as e:
            print(f'  FAIL {fname}: {e}')
            import traceback; traceback.print_exc()

    print('\nDone.')


if __name__ == '__main__':
    main()
