import React from 'react';

interface PatchyProps {
  size?: number;
  status?: 'healthy' | 'incident';
  className?: string;
}

export const PatchyIcon: React.FC<PatchyProps> = ({ 
  size = 32, 
  status = 'healthy',
  className = '' 
}) => {
  const isHealthy = status === 'healthy';
  const eyeColor = isHealthy ? '#38C2DE' : '#EF4444'; // Cyan for clear, Red for incident
  const glowColor = isHealthy ? 'rgba(56, 194, 222, 0.4)' : 'rgba(239, 68, 68, 0.4)';

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      {/* Glow Backdrop */}
      <circle cx="32" cy="32" r="28" fill={glowColor} />

      {/* Antenna */}
      <line x1="32" y1="14" x2="32" y2="8" stroke="#8E95A2" strokeWidth="3" strokeLinecap="round" />
      <circle cx="32" cy="6" r="4" fill={eyeColor} />

      {/* Robot Head */}
      <rect x="14" y="14" width="36" height="30" rx="10" fill="#121316" stroke="#2A2A32" strokeWidth="2" />
      
      {/* Ears / Side Bolts */}
      <rect x="10" y="24" width="4" height="10" rx="2" fill="#8E95A2" />
      <rect x="50" y="24" width="4" height="10" rx="2" fill="#8E95A2" />

      {/* Visor Screen */}
      <rect x="18" y="18" width="28" height="22" rx="6" fill="#0C1016" stroke="#1F242D" />

      {/* LED Eyes */}
      <circle cx="25" cy="28" r="3.5" fill={eyeColor} />
      <circle cx="39" cy="28" r="3.5" fill={eyeColor} />

      {/* Mouth */}
      {isHealthy ? (
        // Happy Smile
        <path d="M26 33 Q32 38 38 33" stroke={eyeColor} strokeWidth="2" strokeLinecap="round" fill="none" />
      ) : (
        // O-shape / Worried Mouth
        <circle cx="32" cy="34" r="2.5" fill={eyeColor} />
      )}

      {/* Tiny Paramedic Cross Badge on Body */}
      <path d="M22 48 H42 V58 H22 Z" fill="#121316" stroke="#2A2A32" />
      <path d="M32 50 V56 M29 53 H35" stroke={eyeColor} strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
};

export default PatchyIcon;