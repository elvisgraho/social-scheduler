def parse_log_data(lines):
    """Parse log lines and return formatted table rows."""
    import re
    
    level_styles = {
        'error': 'background:rgba(239,68,68,0.15);color:var(--error);',
        'warning': 'background:rgba(245,158,11,0.15);color:var(--warning);',
        'success': 'background:rgba(34,197,94,0.15);color:var(--success);',
        'debug': 'background:rgba(6,182,212,0.15);color:var(--debug);',
        'info': 'background:rgba(90,106,240,0.15);color:var(--primary);',
    }
    
    html_parts = ['<table style="width:100%;border-collapse:collapse;font-family:\'SF Mono\',Monaco,monospace;font-size:0.75rem;border-spacing:0;">']
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Extract timestamp
        ts_match = re.match(r'^(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:,\d{3})?)', line)
        if ts_match:
            timestamp = ts_match.group(1).replace(',', '.')
            content = line[len(ts_match.group(0)):].strip()
        else:
            timestamp = ''
            content = line
        
        # Determine log level
        log_level = 'info'
        for pattern, level in [('ERROR', 'error'), ('CRITICAL', 'error'), ('WARNING', 'warning'), ('WARN', 'warning'), ('SUCCESS', 'success'), ('INFO', 'info'), ('DEBUG', 'debug')]:
            if re.search(r'\b' + pattern + r'\b', content, re.IGNORECASE):
                log_level = level
                break
        
        # Escape HTML in content
        content_escaped = content.replace('&', '&').replace('<', '<').replace('>', '>')
        
        style = level_styles.get(log_level, level_styles['info'])
        
        html_parts.append(
            f'<tr>'
            f'<td style="width:85px;padding:0.25rem 0.5rem;color:var(--text-secondary);font-size:0.7rem;white-space:nowrap;border-bottom:1px solid var(--border-light);">{timestamp}</td>'
            f'<td style="width:55px;padding:0.25rem 0.25rem;border-bottom:1px solid var(--border-light);"><span style="display:block;padding:0.1rem 0.25rem;border-radius:3px;font-size:0.65rem;font-weight:600;text-transform:uppercase;text-align:center;white-space:nowrap;{style}">{log_level.upper()}</span></td>'
            f'<td style="padding:0.25rem 0.5rem;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid var(--border-light);">{content_escaped}</td>'
            f'</tr>'
        )
    
    html_parts.append('</table>')
    
    return html_parts if len(html_parts) > 2 else ['<div style="padding:2rem;text-align:center;color:var(--text-secondary);">No logs available</div>']
