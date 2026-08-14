import { useEffect } from 'react';

interface FaviconOptions {
  activeIncidentsCount: number;
}

export function useDynamicFavicon({ activeIncidentsCount }: FaviconOptions) {
  useEffect(() => {
    const hasIncidents = activeIncidentsCount > 0;
    
    // Status colors matching your dashboard theme
    const eyeColor = hasIncidents ? '#EF4444' : '#38C2DE';
    const glowColor = hasIncidents ? 'rgba(239, 68, 68, 0.35)' : 'rgba(56, 194, 222, 0.35)';
    
    // Dynamic Mouth Expression
    const mouthPath = hasIncidents
      ? `<circle cx="32" cy="34" r="2.5" fill="${eyeColor}" />` // Worried / Alert O-mouth
      : `<path d="M26 33 Q32 38 38 33" stroke="${eyeColor}" stroke-width="2" stroke-linecap="round" fill="none" />`; // Happy Smile

    // Raw Patchy SVG
    const svgString = `
      <svg width="64" height="64" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="32" cy="32" r="28" fill="${glowColor}" />
        <line x1="32" y1="14" x2="32" y2="8" stroke="#8E95A2" stroke-width="3" stroke-linecap="round" />
        <circle cx="32" cy="6" r="4" fill="${eyeColor}" />
        <rect x="14" y="14" width="36" height="30" rx="10" fill="#121316" stroke="#2A2A32" stroke-width="2" />
        <rect x="10" y="24" width="4" height="10" rx="2" fill="#8E95A2" />
        <rect x="50" y="24" width="4" height="10" rx="2" fill="#8E95A2" />
        <rect x="18" y="18" width="28" height="22" rx="6" fill="#0C1016" stroke="#1F242D" />
        <circle cx="25" cy="28" r="3.5" fill="${eyeColor}" />
        <circle cx="39" cy="28" r="3.5" fill="${eyeColor}" />
        ${mouthPath}
        <path d="M22 48 H42 V58 H22 Z" fill="#121316" stroke="#2A2A32" />
        <path d="M32 50 V56 M29 53 H35" stroke="${eyeColor}" stroke-width="2" stroke-linecap="round" />
      </svg>
    `.trim();

    // Convert SVG to Data URL
    const encodedSvg = encodeURIComponent(svgString);
    const faviconUrl = `data:image/svg+xml,${encodedSvg}`;

    // Update <link rel="icon">
    let link: HTMLLinkElement | null = document.querySelector("link[rel*='icon']");
    if (!link) {
      link = document.createElement('link');
      link.rel = 'icon';
      document.head.appendChild(link);
    }
    link.type = 'image/svg+xml';
    link.href = faviconUrl;

    // Update Tab Title with Badge Count
    const baseTitle = 'errAgent Dashboard';
    document.title = hasIncidents 
      ? `🚨 (${activeIncidentsCount}) ${baseTitle}` 
      : `🟢 ${baseTitle}`;

  }, [activeIncidentsCount]);
}