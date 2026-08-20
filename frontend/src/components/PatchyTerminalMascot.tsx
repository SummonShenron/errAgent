import React from 'react';

export type PatchyTerminalActivity = 'idle' | 'running' | 'approval' | 'success' | 'warning' | 'error';

type PatchyTerminalMascotProps = {
  activity: PatchyTerminalActivity;
  size?: number;
};

const COLORS: Record<PatchyTerminalActivity, { accent: string; glow: string; visor: string }> = {
  idle: { accent: '#38C2DE', glow: 'rgba(56, 194, 222, 0.14)', visor: '#0C1016' },
  running: { accent: '#60A5FA', glow: 'rgba(96, 165, 250, 0.24)', visor: '#091426' },
  approval: { accent: '#F59E0B', glow: 'rgba(245, 158, 11, 0.24)', visor: '#1c1407' },
  success: { accent: '#38C2DE', glow: 'rgba(56, 194, 222, 0.24)', visor: '#09151c' },
  warning: { accent: '#FBBF24', glow: 'rgba(251, 191, 36, 0.24)', visor: '#1a160a' },
  error: { accent: '#EF4444', glow: 'rgba(239, 68, 68, 0.25)', visor: '#1c0d11' },
};

export const PatchyTerminalMascot: React.FC<PatchyTerminalMascotProps> = ({ activity, size = 108 }) => {
  const { accent, glow, visor } = COLORS[activity];
  const isSuccess = activity === 'success';
  const isRunning = activity === 'running';
  const isApproval = activity === 'approval';
  const isConcerned = activity === 'warning' || activity === 'error';
  const isWaving = activity === 'idle' || activity === 'success';

  const getArmPath = () => {
    if (isWaving) return 'M 76 62 C 84 68, 84 76, 78 82';
    if (isRunning) return 'M76 62 C86 54 84 38 76 31';
    if (isApproval) return 'M76 62 C84 61 84 69 79 75';
    return 'M76 62 C84 68 84 76 78 82';
  };

  const getHandCoords = () => {
    if (isWaving) return { cx: 78, cy: 82 };
    if (isRunning) return { cx: 76, cy: 31 };
    if (isApproval) return { cx: 79, cy: 75 };
    return { cx: 78, cy: 82 };
  };

  const handCoords = getHandCoords();

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={`patchy-terminal-bot patchy-terminal-bot-${activity}`}
      role="img"
      aria-label={`Patchy ${activity}`}
    >
      <defs>
        <clipPath id={`patchy-terminal-visor-${activity}`}>
          <rect x="33" y="25" width="34" height="24" rx="6" />
        </clipPath>
      </defs>

      <g className="patchy-terminal-bot-float">
        <circle cx="50" cy="50" r="39" fill={glow} />

        <circle cx="50" cy="10" r="4" stroke={accent} className="patchy-terminal-radar radar-a" />
        <circle cx="50" cy="10" r="4" stroke={accent} className="patchy-terminal-radar radar-b" />

        <rect x="35" y="85" width="12" height="6" rx="2" fill="#8E95A2" className="patchy-terminal-foot foot-left" />
        <rect x="53" y="85" width="12" height="6" rx="2" fill="#8E95A2" className="patchy-terminal-foot foot-right" />

        <rect x="34" y="56" width="32" height="30" rx="7" fill="#121316" stroke="#2A2A32" strokeWidth="2" />
        <rect x="43" y="63" width="14" height="14" rx="3" fill="#0C1016" stroke="#1F242D" />
        <path d="M50 66 V74 M46 70 H54" stroke={accent} strokeWidth="2" strokeLinecap="round" opacity="0.7" />

        {/* Left Arm (Resting at side) */}
        <path
          d="M24 62 C16 66 16 76 22 82"
          stroke="#8E95A2"
          strokeWidth="4"
          strokeLinecap="round"
          className="patchy-terminal-arm arm-left"
        />
        <circle cx="22" cy="82" r="3.5" fill="#8E95A2" />

        {/* Head Assembly */}
        <g className="patchy-terminal-head">
          <line x1="50" y1="20" x2="50" y2="12" stroke="#8E95A2" strokeWidth="2.5" strokeLinecap="round" />
          <circle cx="50" cy="10" r="4" fill={accent} />
          <rect x="28" y="20" width="44" height="34" rx="10" fill="#121316" stroke="#2A2A32" strokeWidth="2" />
          <rect x="23" y="32" width="5" height="10" rx="2" fill="#8E95A2" />
          <rect x="72" y="32" width="5" height="10" rx="2" fill="#8E95A2" />
          <rect x="33" y="25" width="34" height="24" rx="6" fill={visor} stroke={accent} strokeOpacity="0.34" />

          {isRunning && (
            <g clipPath={`url(#patchy-terminal-visor-${activity})`}>
              <line x1="34" y1="29" x2="66" y2="29" stroke={accent} strokeWidth="1.5" className="patchy-terminal-visor-scan" />
            </g>
          )}

          <g className="patchy-terminal-eyes">
            {isRunning ? (
              <>
                <rect x="38" y="34" width="8" height="2.5" rx="1.25" fill={accent} />
                <rect x="54" y="34" width="8" height="2.5" rx="1.25" fill={accent} />
              </>
            ) : (
              <>
                <circle cx="42" cy="35" r="3.5" fill={accent} />
                <circle cx="58" cy="35" r="3.5" fill={accent} />
              </>
            )}
          </g>

          {isSuccess ? (
            <path d="M41 40 Q50 48 59 40" stroke={accent} strokeWidth="2.5" strokeLinecap="round" />
          ) : isConcerned ? (
            <path d="M43 44 Q50 39 57 44" stroke={accent} strokeWidth="2" strokeLinecap="round" />
          ) : isApproval ? (
            <path d="M43 42 H57" stroke={accent} strokeWidth="2.5" strokeLinecap="round" />
          ) : isRunning ? (
            <path d="M43 42 H47 M49 42 H52 M54 42 H58" stroke={accent} strokeWidth="2" strokeLinecap="round" />
          ) : (
            <path d="M43 41 Q50 46 57 41" stroke={accent} strokeWidth="2" strokeLinecap="round" />
          )}
        </g>

        {/* Right Arm (Waving with SVG Path Morphing) */}
        <g className="patchy-terminal-arm arm-right">
          <path
            d={getArmPath()}
            stroke="#8E95A2"
            strokeWidth="4"
            strokeLinecap="round"
            fill="none"
          >
            {isWaving && (
              <animate
                attributeName="d"
                dur="8s"
                repeatCount="indefinite"
                calcMode="spline"
                keyTimes="0; 0.1; 0.2; 0.3; 0.4; 0.5; 0.6; 1"
                keySplines="0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1"
                values="
                  M 76 62 C 84 68, 84 76, 78 82;
                  M 76 62 C 88 56, 94 42, 92 30;
                  M 76 62 C 82 50, 86 36, 84 26;
                  M 76 62 C 88 56, 94 42, 92 30;
                  M 76 62 C 82 50, 86 36, 84 26;
                  M 76 62 C 88 56, 94 42, 92 30;
                  M 76 62 C 84 68, 84 76, 78 82;
                  M 76 62 C 84 68, 84 76, 78 82
                "
              />
            )}
          </path>

          <circle
            cx={handCoords.cx}
            cy={handCoords.cy}
            r="3.5"
            fill={isWaving ? accent : '#8E95A2'}
          >
            {isWaving && (
              <>
                <animate
                  attributeName="cx"
                  dur="8s"
                  repeatCount="indefinite"
                  calcMode="spline"
                  keyTimes="0; 0.1; 0.2; 0.3; 0.4; 0.5; 0.6; 1"
                  keySplines="0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1"
                  values="78; 92; 84; 92; 84; 92; 78; 78"
                />
                <animate
                  attributeName="cy"
                  dur="8s"
                  repeatCount="indefinite"
                  calcMode="spline"
                  keyTimes="0; 0.1; 0.2; 0.3; 0.4; 0.5; 0.6; 1"
                  keySplines="0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1; 0.25 1 0.5 1"
                  values="82; 30; 26; 30; 26; 30; 82; 82"
                />
              </>
            )}
          </circle>
        </g>
      </g>
    </svg>
  );
};

export default PatchyTerminalMascot;