#!/bin/bash
# Batch Test Script
# Tests all 8 styles with sample diagrams

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="${SKILL_DIR}/test-output"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo -e "${BLUE}=== Fireworks Tech Graph - Batch Test ===${NC}"
echo "Test directory: $TEST_DIR"
echo "Timestamp: $TIMESTAMP"
echo ""

# Create test directory
mkdir -p "$TEST_DIR"

# Test configuration
STYLES=(1 2 3 4 5 6 7)
STYLE_NAMES=("Flat Icon" "Dark Terminal" "Blueprint" "Notion Clean" "Glassmorphism" "Claude Official" "OpenAI Official")

# Summary counters
TOTAL=0
PASSED=0
FAILED=0

echo -e "${BLUE}Testing all styles...${NC}"
echo "----------------------------------------"

for i in "${!STYLES[@]}"; do
    STYLE="${STYLES[$i]}"
    STYLE_NAME="${STYLE_NAMES[$i]}"
    
    echo -e "\n${YELLOW}Style $STYLE: $STYLE_NAME${NC}"
    
    # Check if style reference exists
    STYLE_FILE="${SKILL_DIR}/references/style-${STYLE}.md"
    if [ ! -f "$STYLE_FILE" ]; then
        echo -e "${RED}✗ Style file not found: $STYLE_FILE${NC}"
        FAILED=$((FAILED + 1))
        TOTAL=$((TOTAL + 1))
        continue
    fi
    
    echo -e "${GREEN}✓ Style file found${NC}"
    
    # Check for sample SVG files
    SAMPLE_FILES=$(find "$SKILL_DIR" -name "*style${STYLE}*.svg" -o -name "*style-${STYLE}*.svg" 2>/dev/null || true)
    
    if [ -z "$SAMPLE_FILES" ]; then
        echo -e "${YELLOW}⚠ No sample SVG files found for style $STYLE${NC}"
        continue
    fi
    
    # Validate each sample file
    for SVG_FILE in $SAMPLE_FILES; do
        BASENAME=$(basename "$SVG_FILE")
        echo -n "  Testing $BASENAME... "
        
        TOTAL=$((TOTAL + 1))
        
        # Run validation
        if "${SKILL_DIR}/scripts/validate-svg.sh" "$SVG_FILE" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Pass${NC}"
            PASSED=$((PASSED + 1))
            
            # Try to export PNG
            PNG_FILE="${TEST_DIR}/${BASENAME%.svg}_${TIMESTAMP}.png"
            if command -v rsvg-convert &> /dev/null; then
                if rsvg-convert -w 1920 "$SVG_FILE" -o "$PNG_FILE" 2>/dev/null; then
                    PNG_SIZE=$(du -h "$PNG_FILE" | cut -f1)
                    echo "    PNG exported: ${PNG_SIZE}"
                fi
            fi
        else
            echo -e "${RED}✗ Fail${NC}"
            FAILED=$((FAILED + 1))
            
            # Show validation errors
            "${SKILL_DIR}/scripts/validate-svg.sh" "$SVG_FILE" 2>&1 | grep -E "✗|Error" | sed 's/^/    /'
        fi
    done
done

# Print summary
echo ""
echo "========================================"
echo -e "${BLUE}Test Summary${NC}"
echo "----------------------------------------"
echo "Total tests: $TOTAL"
echo -e "${GREEN}Passed: $PASSED${NC}"
echo -e "${RED}Failed: $FAILED${NC}"

if [ "$FAILED" -eq 0 ]; then
    echo -e "\n${GREEN}✓ All tests passed!${NC}"
    exit 0
else
    echo -e "\n${RED}✗ Some tests failed${NC}"
    exit 1
fi
