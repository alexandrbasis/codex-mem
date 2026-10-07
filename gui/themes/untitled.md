# Untitled

Generated from the current project, including edits awaiting autosave. Return to the [theme index](../themes.md). Font names and weights are references only: license, download, and configure your own fonts.

## Foundations

```json
{
  "name": "Untitled",
  "text": {
    "l": {
      "size": 24,
      "lineHeight": 32,
      "letterSpacing": 0
    },
    "m": {
      "size": 16,
      "lineHeight": 24,
      "letterSpacing": 0
    },
    "s": {
      "size": 14,
      "lineHeight": 20,
      "letterSpacing": 0
    },
    "xl": {
      "size": 36,
      "lineHeight": 40,
      "letterSpacing": 0
    },
    "xs": {
      "size": 12,
      "lineHeight": 16,
      "letterSpacing": 0
    },
    "xxl": {
      "size": 48,
      "lineHeight": 52,
      "letterSpacing": 0
    },
    "xxs": {
      "size": 10,
      "lineHeight": 14,
      "letterSpacing": 0
    }
  },
  "fonts": {
    "ui": {
      "family": "\"Timeless Grotesk\", sans-serif",
      "weights": {
        "heavy": 600,
        "medium": 500,
        "regular": 400
      }
    },
    "data": {
      "family": "\"Timeless Grotesk\", sans-serif",
      "weights": {
        "heavy": 600,
        "medium": 500,
        "regular": 400
      }
    },
    "brand": {
      "family": "\"Timeless Grotesk\", sans-serif",
      "weights": {
        "heavy": 600,
        "medium": 500,
        "regular": 400
      }
    },
    "editorial": {
      "family": "\"Timeless Grotesk\", sans-serif",
      "weights": {
        "heavy": 600,
        "medium": 500,
        "regular": 400
      }
    }
  },
  "border": {
    "l": 0,
    "m": 0,
    "s": 0,
    "none": 0
  },
  "radius": {
    "l": 18,
    "m": 12,
    "s": 8,
    "xl": 28,
    "xs": 4,
    "full": 9999,
    "zero": 0
  },
  "shadows": {
    "l": {
      "x": 0,
      "y": 16,
      "blur": 48,
      "color": {
        "dark": "neutral-1",
        "light": "neutral-10"
      },
      "spread": 0,
      "opacity": 0
    },
    "m": {
      "x": 0,
      "y": 8,
      "blur": 24,
      "color": {
        "dark": "neutral-1",
        "light": "neutral-10"
      },
      "spread": 0,
      "opacity": 12
    },
    "s": {
      "x": 0,
      "y": 2,
      "blur": 4,
      "color": {
        "dark": "neutral-1",
        "light": "neutral-10"
      },
      "spread": 0,
      "opacity": 0
    }
  },
  "spacing": {
    "l": 24,
    "m": 16,
    "s": 12,
    "xl": 32,
    "xs": 8,
    "xxl": 48,
    "xxs": 4,
    "zero": 0
  },
  "animation": {
    "large": {
      "easing": [
        0.22,
        1,
        0.36,
        1
      ],
      "duration": 360
    },
    "easing": [
      0.16,
      1,
      0.3,
      1
    ],
    "duration": 200,
    "popupScale": 0.96,
    "pressDistance": 1
  },
  "iconStyle": "outlined",
  "iconFamily": "Lucide",
  "neutralTone": "neutral",
  "buttonRadius": "s",
  "colorEmphasis": 52,
  "primaryForeground": {
    "dark": "neutral-9",
    "light": "neutral-2"
  },
  "primaryActionColor": "color-1"
}
```

## light CSS variables

Define these in the app’s existing theme scope for this mode. Keep component styles linked to the variables.

| Variable | Value |
| --- | --- |
| `--theme-name` | Untitled |
| `--theme-icon-family` | Lucide |
| `--theme-icon-style` | outlined |
| `--toolbar-divider-bleed` | 0 |
| `--focus-ring-outline` | initial |
| `--icon-stroke-width` | 2 |
| `--icon-light-display` | none |
| `--icon-regular-display` | inline |
| `--icon-bold-display` | none |
| `--motion-duration` | 200ms |
| `--motion-easing` | cubic-bezier(0.16, 1, 0.3, 1) |
| `--motion-type` | easing |
| `--motion-visual-duration` | 0.2 |
| `--motion-bounce` | 0.2 |
| `--motion-enabled` | 1 |
| `--motion-small-iterations` | infinite |
| `--motion-large-duration` | 360ms |
| `--motion-large-easing` | cubic-bezier(0.22, 1, 0.36, 1) |
| `--motion-large-type` | easing |
| `--motion-large-visual-duration` | 0.36 |
| `--motion-large-bounce` | 0.2 |
| `--motion-large-iterations` | infinite |
| `--motion-popup-scale` | 0.96 |
| `--motion-press-distance` | 1px |
| `--option-badge-background` | #07376b |
| `--option-badge-foreground` | #fbfbfb |
| `--navigation-active-foreground` | #fbfbfb |
| `--emphasis-chart-fill` | #07376b33 |
| `--emphasis-balance-background` | #cdcdcd |
| `--emphasis-rewards-background` | #f4f4f4 |
| `--emphasis-icon-background` | #f4f4f4 |
| `--emphasis-icon-foreground` | #000000 |
| `--emphasis-type-background` | #f4f4f4 |
| `--emphasis-type-foreground` | #000000 |
| `--navigation-active-background` | #07376b |
| `--surface-raised-image` | none |
| `--surface-raised-shadow` | 0 0 0 0 transparent |
| `--surface-recessed-image` | none |
| `--surface-recessed-shadow` | 0 0 0 0 transparent |
| `--space-zero` | 0px |
| `--space-xxs` | 4px |
| `--space-xs` | 8px |
| `--space-s` | 12px |
| `--space-m` | 16px |
| `--space-l` | 24px |
| `--space-xl` | 32px |
| `--space-xxl` | 48px |
| `--size-xxs` | 10px |
| `--line-xxs` | 14px |
| `--letter-spacing-xxs` | 0em |
| `--size-xs` | 12px |
| `--line-xs` | 16px |
| `--letter-spacing-xs` | 0em |
| `--size-s` | 14px |
| `--line-s` | 20px |
| `--letter-spacing-s` | 0em |
| `--size-m` | 16px |
| `--line-m` | 24px |
| `--letter-spacing-m` | 0em |
| `--size-l` | 24px |
| `--line-l` | 32px |
| `--letter-spacing-l` | 0em |
| `--size-xl` | 36px |
| `--line-xl` | 40px |
| `--letter-spacing-xl` | 0em |
| `--size-xxl` | 48px |
| `--line-xxl` | 52px |
| `--letter-spacing-xxl` | 0em |
| `--radius-zero` | 0px |
| `--radius-xs` | 4px |
| `--radius-s` | 8px |
| `--radius-m` | 12px |
| `--radius-l` | 18px |
| `--radius-xl` | 28px |
| `--radius-full` | 9999px |
| `--border-none` | 0px |
| `--border-s` | 0px |
| `--border-m` | 0px |
| `--border-l` | 0px |
| `--border-default-color` | #e7e7e733 |
| `--border-shadow-none` | 0 0 0 0 transparent |
| `--border-shadow-s` | 0 0 0 0 transparent |
| `--border-shadow-m` | 0 0 0 0 transparent |
| `--border-shadow-l` | 0 0 0 0 transparent |
| `--font-ui` | "Timeless Grotesk", sans-serif |
| `--weight-ui-regular` | 400 |
| `--weight-ui-medium` | 500 |
| `--weight-ui-heavy` | 600 |
| `--font-brand` | "Timeless Grotesk", sans-serif |
| `--weight-brand-regular` | 400 |
| `--weight-brand-medium` | 500 |
| `--weight-brand-heavy` | 600 |
| `--font-editorial` | "Timeless Grotesk", sans-serif |
| `--weight-editorial-regular` | 400 |
| `--weight-editorial-medium` | 500 |
| `--weight-editorial-heavy` | 600 |
| `--font-data` | "Timeless Grotesk", sans-serif |
| `--weight-data-regular` | 400 |
| `--weight-data-medium` | 500 |
| `--weight-data-heavy` | 600 |
| `--color-none` | transparent |
| `--color-1` | #07376b |
| `--color-1-transparent` | #07376b33 |
| `--color-2` | #ff5a00 |
| `--color-2-transparent` | #ff5a0033 |
| `--color-3` | #87d8f5 |
| `--color-3-transparent` | #87d8f533 |
| `--color-4` | #ffed9b |
| `--color-4-transparent` | #ffed9b33 |
| `--neutral-1` | #ffffff |
| `--neutral-1-transparent` | #ffffff33 |
| `--neutral-2` | #fbfbfb |
| `--neutral-2-transparent` | #fbfbfb33 |
| `--neutral-3` | #f4f4f4 |
| `--neutral-3-transparent` | #f4f4f433 |
| `--neutral-4` | #e7e7e7 |
| `--neutral-4-transparent` | #e7e7e733 |
| `--neutral-5` | #cdcdcd |
| `--neutral-5-transparent` | #cdcdcd33 |
| `--neutral-6` | #a1a1a1 |
| `--neutral-6-transparent` | #a1a1a133 |
| `--neutral-7` | #7d7d7d |
| `--neutral-7-transparent` | #7d7d7d33 |
| `--neutral-8` | #555555 |
| `--neutral-8-transparent` | #55555533 |
| `--neutral-9` | #333333 |
| `--neutral-9-transparent` | #33333333 |
| `--neutral-10` | #000000 |
| `--neutral-10-transparent` | #00000033 |
| `--success` | #00906c |
| `--success-transparent` | #00906c33 |
| `--warning` | #ffea00 |
| `--warning-transparent` | #ffea0033 |
| `--error` | #fc032d |
| `--error-transparent` | #fc032d33 |
| `--shadow-none` | none |
| `--shadow-s` | 0px 2px 4px 0px #00000000 |
| `--shadow-m` | 0px 2px 6px 0px #0000000d, 0px 8px 24px 0px #00000013 |
| `--shadow-l` | 0px 16px 48px 0px #00000000 |
| `--cte-canvas` | #ffffff |
| `--cte-surface` | #fbfbfb |
| `--cte-surface-muted` | #f4f4f4 |
| `--cte-text` | #000000 |
| `--cte-text-muted` | #7d7d7d |
| `--cte-border` | #e7e7e733 |
| `--cte-accent` | #07376b |
| `--cte-accent-text` | #fbfbfb |
| `--cte-danger` | #fc032d |
| `--cte-focus` | #07376b |
| `--cte-font` | "Timeless Grotesk", sans-serif |
| `--cte-font-size` | 14px |
| `--cte-font-weight` | 400 |
| `--cte-line-height` | 20px |
| `--cte-letter-spacing` | 0em |
| `--cte-detail-font-size` | 12px |
| `--cte-detail-line-height` | 16px |
| `--cte-detail-letter-spacing` | 0em |

## dark CSS variables

Define these in the app’s existing theme scope for this mode. Keep component styles linked to the variables.

| Variable | Value |
| --- | --- |
| `--theme-name` | Untitled |
| `--theme-icon-family` | Lucide |
| `--theme-icon-style` | outlined |
| `--toolbar-divider-bleed` | 0 |
| `--focus-ring-outline` | initial |
| `--icon-stroke-width` | 2 |
| `--icon-light-display` | none |
| `--icon-regular-display` | inline |
| `--icon-bold-display` | none |
| `--motion-duration` | 200ms |
| `--motion-easing` | cubic-bezier(0.16, 1, 0.3, 1) |
| `--motion-type` | easing |
| `--motion-visual-duration` | 0.2 |
| `--motion-bounce` | 0.2 |
| `--motion-enabled` | 1 |
| `--motion-small-iterations` | infinite |
| `--motion-large-duration` | 360ms |
| `--motion-large-easing` | cubic-bezier(0.22, 1, 0.36, 1) |
| `--motion-large-type` | easing |
| `--motion-large-visual-duration` | 0.36 |
| `--motion-large-bounce` | 0.2 |
| `--motion-large-iterations` | infinite |
| `--motion-popup-scale` | 0.96 |
| `--motion-press-distance` | 1px |
| `--option-badge-background` | #07376b |
| `--option-badge-foreground` | #fbfbfb |
| `--navigation-active-foreground` | #fbfbfb |
| `--emphasis-chart-fill` | #07376b33 |
| `--emphasis-balance-background` | #565656 |
| `--emphasis-rewards-background` | #2b2b2b |
| `--emphasis-icon-background` | #2b2b2b |
| `--emphasis-icon-foreground` | #ffffff |
| `--emphasis-type-background` | #2b2b2b |
| `--emphasis-type-foreground` | #ffffff |
| `--navigation-active-background` | #07376b |
| `--surface-raised-image` | none |
| `--surface-raised-shadow` | 0 0 0 0 transparent |
| `--surface-recessed-image` | none |
| `--surface-recessed-shadow` | 0 0 0 0 transparent |
| `--space-zero` | 0px |
| `--space-xxs` | 4px |
| `--space-xs` | 8px |
| `--space-s` | 12px |
| `--space-m` | 16px |
| `--space-l` | 24px |
| `--space-xl` | 32px |
| `--space-xxl` | 48px |
| `--size-xxs` | 10px |
| `--line-xxs` | 14px |
| `--letter-spacing-xxs` | 0em |
| `--size-xs` | 12px |
| `--line-xs` | 16px |
| `--letter-spacing-xs` | 0em |
| `--size-s` | 14px |
| `--line-s` | 20px |
| `--letter-spacing-s` | 0em |
| `--size-m` | 16px |
| `--line-m` | 24px |
| `--letter-spacing-m` | 0em |
| `--size-l` | 24px |
| `--line-l` | 32px |
| `--letter-spacing-l` | 0em |
| `--size-xl` | 36px |
| `--line-xl` | 40px |
| `--letter-spacing-xl` | 0em |
| `--size-xxl` | 48px |
| `--line-xxl` | 52px |
| `--letter-spacing-xxl` | 0em |
| `--radius-zero` | 0px |
| `--radius-xs` | 4px |
| `--radius-s` | 8px |
| `--radius-m` | 12px |
| `--radius-l` | 18px |
| `--radius-xl` | 28px |
| `--radius-full` | 9999px |
| `--border-none` | 0px |
| `--border-s` | 0px |
| `--border-m` | 0px |
| `--border-l` | 0px |
| `--border-default-color` | #3d3d3d33 |
| `--border-shadow-none` | 0 0 0 0 transparent |
| `--border-shadow-s` | 0 0 0 0 transparent |
| `--border-shadow-m` | 0 0 0 0 transparent |
| `--border-shadow-l` | 0 0 0 0 transparent |
| `--font-ui` | "Timeless Grotesk", sans-serif |
| `--weight-ui-regular` | 400 |
| `--weight-ui-medium` | 500 |
| `--weight-ui-heavy` | 600 |
| `--font-brand` | "Timeless Grotesk", sans-serif |
| `--weight-brand-regular` | 400 |
| `--weight-brand-medium` | 500 |
| `--weight-brand-heavy` | 600 |
| `--font-editorial` | "Timeless Grotesk", sans-serif |
| `--weight-editorial-regular` | 400 |
| `--weight-editorial-medium` | 500 |
| `--weight-editorial-heavy` | 600 |
| `--font-data` | "Timeless Grotesk", sans-serif |
| `--weight-data-regular` | 400 |
| `--weight-data-medium` | 500 |
| `--weight-data-heavy` | 600 |
| `--color-none` | transparent |
| `--color-1` | #07376b |
| `--color-1-transparent` | #07376b33 |
| `--color-2` | #ff5a00 |
| `--color-2-transparent` | #ff5a0033 |
| `--color-3` | #87d8f5 |
| `--color-3-transparent` | #87d8f533 |
| `--color-4` | #ffed9b |
| `--color-4-transparent` | #ffed9b33 |
| `--neutral-1` | #000000 |
| `--neutral-1-transparent` | #00000033 |
| `--neutral-2` | #202020 |
| `--neutral-2-transparent` | #20202033 |
| `--neutral-3` | #2b2b2b |
| `--neutral-3-transparent` | #2b2b2b33 |
| `--neutral-4` | #3d3d3d |
| `--neutral-4-transparent` | #3d3d3d33 |
| `--neutral-5` | #565656 |
| `--neutral-5-transparent` | #56565633 |
| `--neutral-6` | #838383 |
| `--neutral-6-transparent` | #83838333 |
| `--neutral-7` | #ababab |
| `--neutral-7-transparent` | #ababab33 |
| `--neutral-8` | #cfcfcf |
| `--neutral-8-transparent` | #cfcfcf33 |
| `--neutral-9` | #f4f4f4 |
| `--neutral-9-transparent` | #f4f4f433 |
| `--neutral-10` | #ffffff |
| `--neutral-10-transparent` | #ffffff33 |
| `--success` | #00906c |
| `--success-transparent` | #00906c33 |
| `--warning` | #ffea00 |
| `--warning-transparent` | #ffea0033 |
| `--error` | #fc032d |
| `--error-transparent` | #fc032d33 |
| `--shadow-none` | none |
| `--shadow-s` | 0px 2px 4px 0px #00000000 |
| `--shadow-m` | 0px 2px 6px 0px #0000000d, 0px 8px 24px 0px #00000013 |
| `--shadow-l` | 0px 16px 48px 0px #00000000 |
| `--cte-canvas` | #000000 |
| `--cte-surface` | #202020 |
| `--cte-surface-muted` | #2b2b2b |
| `--cte-text` | #ffffff |
| `--cte-text-muted` | #ababab |
| `--cte-border` | #3d3d3d33 |
| `--cte-accent` | #07376b |
| `--cte-accent-text` | #f4f4f4 |
| `--cte-danger` | #fc032d |
| `--cte-focus` | #07376b |
| `--cte-font` | "Timeless Grotesk", sans-serif |
| `--cte-font-size` | 14px |
| `--cte-font-weight` | 400 |
| `--cte-line-height` | 20px |
| `--cte-letter-spacing` | 0em |
| `--cte-detail-font-size` | 12px |
| `--cte-detail-line-height` | 16px |
| `--cte-detail-letter-spacing` | 0em |

## Authored component assignments

These are project edits. The [component reference](untitled-components.md) includes the effective assignments with defaults and shared parts resolved.

```json
{
  "componentTokens": {
    "button:ghost:rest": {
      "paddingX": "l",
      "paddingTop": "s",
      "paddingBottom": "s"
    },
    "button:danger:rest": {
      "paddingX": "l",
      "paddingTop": "s",
      "paddingBottom": "s"
    },
    "input:default:rest": {
      "paddingX": "m",
      "background": "neutral-3",
      "paddingTop": "s",
      "paddingBottom": "s"
    },
    "button:outline:rest": {
      "paddingX": "l",
      "paddingTop": "s",
      "paddingBottom": "s"
    },
    "button:primary:rest": {
      "paddingX": "l",
      "paddingTop": "s",
      "paddingBottom": "s"
    },
    "select:default:rest": {
      "paddingX": "m",
      "background": "neutral-3",
      "paddingTop": "s",
      "paddingBottom": "s"
    },
    "button:secondary:rest": {
      "paddingX": "l",
      "paddingTop": "s",
      "paddingBottom": "s"
    },
    "combobox:default:rest": {
      "paddingX": "m",
      "background": "neutral-3",
      "paddingTop": "s",
      "paddingBottom": "s"
    },
    "menu:default:part:option:rest": {
      "paddingX": "xs",
      "paddingTop": "xs",
      "paddingLeft": "xs",
      "paddingRight": "xs",
      "paddingBottom": "xs"
    },
    "slider:default:part:thumb:rest": {
      "controlSize": "l"
    },
    "slider:default:part:track:rest": {
      "controlSize": "xl"
    },
    "switch:default:part:control:rest": {
      "controlSize": "xl"
    },
    "combobox:default:part:option:rest": {
      "paddingX": "xs",
      "paddingTop": "xs",
      "paddingLeft": "xs",
      "paddingRight": "xs",
      "paddingBottom": "xs"
    },
    "menu:default:part:option:selected": {
      "paddingX": "xs",
      "paddingTop": "xs",
      "paddingLeft": "xs",
      "paddingRight": "xs",
      "paddingBottom": "xs"
    },
    "otp-field:default:part:input:rest": {
      "paddingX": "s",
      "paddingTop": "xs",
      "paddingLeft": "s",
      "paddingRight": "s",
      "paddingBottom": "xs"
    },
    "checkbox:default:part:control:rest": {
      "controlSize": "l"
    },
    "autocomplete:default:part:input:rest": {
      "paddingX": "s",
      "paddingTop": "s",
      "paddingLeft": "s",
      "paddingRight": "s",
      "paddingBottom": "s"
    },
    "autocomplete:default:part:option:rest": {
      "paddingX": "xs",
      "paddingTop": "xs",
      "paddingLeft": "xs",
      "paddingRight": "xs",
      "paddingBottom": "xs"
    },
    "combobox:default:part:option:selected": {
      "paddingX": "xs",
      "paddingTop": "xs",
      "paddingLeft": "xs",
      "paddingRight": "xs",
      "paddingBottom": "xs"
    },
    "autocomplete:default:part:popover:rest": {
      "radius": "s"
    },
    "autocomplete:default:part:option:selected": {
      "paddingX": "xs",
      "paddingTop": "xs",
      "paddingLeft": "xs",
      "paddingRight": "xs",
      "paddingBottom": "xs"
    }
  },
  "componentVariants": {}
}
```
