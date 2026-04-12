#!/usr/bin/env python3
"""
Template-based SVG Generator
Usage: python3 generate-from-template.py <template-type> <output-path> <data-json>
"""

import sys
import json
import os

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), '..', 'templates')

def load_template(template_type):
    template_path = os.path.join(TEMPLATE_DIR, f'{template_type}.svg')
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template not found: {template_path}")
    with open(template_path, 'r') as f:
        return f.read()

def generate_node(node_data):
    # Validate required fields
    required = ['x', 'y', 'label']
    missing = [k for k in required if k not in node_data]
    if missing:
        raise ValueError(f"Node missing required fields: {missing}")

    shape = node_data.get('shape', 'rect')
    x, y = node_data['x'], node_data['y']
    fill = node_data.get('fill', '#dbeafe')
    stroke = node_data.get('stroke', '#2563eb')
    label = node_data['label']
    sublabel = node_data.get('sublabel', '')
    
    svg = ''
    if shape == 'circle':
        r = node_data.get('r', 50)
        svg = f'  <circle cx="{x}" cy="{y}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="2.5"/>\n'
        svg += f'  <text x="{x}" y="{y-5}" text-anchor="middle" class="node-label">{label}</text>\n'
        if sublabel:
            svg += f'  <text x="{x}" y="{y+15}" text-anchor="middle" class="sub-label">{sublabel}</text>\n'
    elif shape == 'rect':
        w = node_data.get('width', 180)
        h = node_data.get('height', 80)
        rx = node_data.get('rx', 8)
        svg = f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="2.5"/>\n'
        label_y = y + h//2 - 5
        svg += f'  <text x="{x + w//2}" y="{label_y}" text-anchor="middle" class="node-label">{label}</text>\n'
        if sublabel:
            svg += f'  <text x="{x + w//2}" y="{label_y + 20}" text-anchor="middle" class="sub-label">{sublabel}</text>\n'
    elif shape == 'ellipse':
        rx = node_data.get('rx', 90)
        ry = node_data.get('ry', 50)
        svg = f'  <ellipse cx="{x}" cy="{y}" rx="{rx}" ry="{ry}" fill="{fill}" stroke="{stroke}" stroke-width="2.5"/>\n'
        svg += f'  <text x="{x}" y="{y-5}" text-anchor="middle" class="node-label">{label}</text>\n'
        if sublabel:
            svg += f'  <text x="{x}" y="{y+15}" text-anchor="middle" class="sub-label">{sublabel}</text>\n'
    return svg

def generate_arrow(arrow_data):
    # Validate required fields
    required = ['x1', 'y1', 'x2', 'y2']
    missing = [k for k in required if k not in arrow_data]
    if missing:
        raise ValueError(f"Arrow missing required fields: {missing}")

    x1, y1 = arrow_data['x1'], arrow_data['y1']
    x2, y2 = arrow_data['x2'], arrow_data['y2']
    color = arrow_data.get('color', '#2563eb')
    label = arrow_data.get('label', '')
    arrow_type = arrow_data.get('type', 'line')
    
    color_map = {'#2563eb': 'arrowBlue', '#059669': 'arrowGreen', '#7c3aed': 'arrowPurple', '#ea580c': 'arrowOrange'}
    marker_id = color_map.get(color, 'arrowBlue')
    
    svg = ''
    if arrow_type == 'line':
        svg = f'  <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="2" marker-end="url(#{marker_id})"/>\n'
    elif arrow_type == 'path':
        d = arrow_data['d']
        svg = f'  <path d="{d}" fill="none" stroke="{color}" stroke-width="2" marker-end="url(#{marker_id})"/>\n'
    
    if label:
        label_x = (x1 + x2) // 2
        label_y = (y1 + y2) // 2
        label_width = len(label) * 7 + 8
        svg += f'  <rect x="{label_x - label_width//2}" y="{label_y - 9}" width="{label_width}" height="18" fill="#ffffff" opacity="0.95"/>\n'
        svg += f'  <text x="{label_x}" y="{label_y + 4}" text-anchor="middle" class="arrow-label">{label}</text>\n'
    return svg

def generate_svg(template_type, data):
    template = load_template(template_type)
    
    nodes_svg = ''.join(generate_node(node) for node in data.get('nodes', []))
    arrows_svg = ''.join(generate_arrow(arrow) for arrow in data.get('arrows', []))
    
    legend_svg = ''
    for i, legend_item in enumerate(data.get('legend', [])):
        y = 65 + i * 25
        color = legend_item['color']
        label = legend_item['label']
        color_map = {'#2563eb': 'arrowBlue', '#059669': 'arrowGreen', '#7c3aed': 'arrowPurple', '#ea580c': 'arrowOrange'}
        marker_id = color_map.get(color, 'arrowBlue')
        legend_svg += f'  <line x1="750" y1="{y}" x2="780" y2="{y}" stroke="{color}" stroke-width="2" marker-end="url(#{marker_id})"/>\n'
        legend_svg += f'  <text x="790" y="{y+5}" class="arrow-label">{label}</text>\n'
    
    title = data.get('title', 'Diagram')
    svg = template.replace('{{TITLE}}', title)
    svg = svg.replace('<!-- NODES -->', nodes_svg)
    svg = svg.replace('<!-- ARROWS -->', arrows_svg)
    svg = svg.replace('<!-- LEGEND -->', legend_svg)
    return svg

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 generate-from-template.py <template-type> <output-path> [data-json]")
        sys.exit(1)

    template_type = sys.argv[1]
    output_path = sys.argv[2]

    try:
        # Parse input data
        if len(sys.argv) > 3:
            try:
                data = json.loads(sys.argv[3])
            except json.JSONDecodeError as e:
                print(f"Error: Invalid JSON in command line argument: {e}")
                sys.exit(1)
        else:
            try:
                data = json.load(sys.stdin)
            except json.JSONDecodeError as e:
                print(f"Error: Invalid JSON from stdin: {e}")
                sys.exit(1)

        # Generate SVG
        svg_content = generate_svg(template_type, data)

        # Write output
        with open(output_path, 'w') as f:
            f.write(svg_content)

        print(f"✓ SVG generated: {output_path}")

    except FileNotFoundError as e:
        print(f"Error: {e}")
        # List available templates
        if os.path.exists(TEMPLATE_DIR):
            templates = [f.replace('.svg', '') for f in os.listdir(TEMPLATE_DIR) if f.endswith('.svg')]
            if templates:
                print(f"Available templates: {', '.join(templates)}")
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
