const tailwindColors = [
    "red",
    "yellow",
    "green",
    "blue",
    "orange",
    "purple",
    "pink",
    "teal",
    "cyan",
    "lime",
    "indigo",
    "fuchsia",
    "rose",
    "sky",
    "amber",
    "emerald",
];

export function convertColorToTextClass(color: string) {
    if (tailwindColors.includes(color)) {
        return `text-${color}-500`;
    }
    return `text-gray-500`;
}

export function convertToBGClass(color: string) {
    if (tailwindColors.includes(color)) {
        return `bg-${color}-500 dark:bg-${color}-900 hover:bg-${color}-400 dark:hover:bg-${color}-800`;
    }
    return `bg-background`;
}
