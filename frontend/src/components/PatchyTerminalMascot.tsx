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

  const getLeftArmPath = () => {
    if (isRunning) return 'M 24 62 C 14 50, 16 35, 24 32';
    return 'M 24 62 C 16 66, 16 76, 22 82';
  };

  const getLeftHandCoords = () => {
    if (isRunning) return { cx: 24, cy: 32 };
    return { cx: 22, cy: 82 };
  };

  const getRightArmPath = () => {
    if (isWaving) return 'M 76 62 C 84 68, 84 76, 78 82';
    if (isRunning) return 'M 76 62 C 86 50, 84 35, 76 32';
    if (isApproval) return 'M 76 62 C 88 42, 72 14, 52 18'; // Reaches behind top-back of head
    return 'M 76 62 C 84 68, 84 76, 78 82';
  };

  const getRightHandCoords = () => {
    if (isWaving) return { cx: 78, cy: 82 };
    if (isRunning) return { cx: 76, cy: 32 };
    if (isApproval) return { cx: 52, cy: 18 }; // Tucked behind back top edge of head
    return { cx: 78, cy: 82 };
  };

  const leftHandCoords = getLeftHandCoords();
  const rightHandCoords = getRightHandCoords();

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
      <style>{`
        /* Squash & Stretch Vertical Float */
        @keyframes patchyFloat {
          0%, 100% {
            transform: translateY(0px) scale(1, 1);
          }
          50% {
            transform: translateY(-5px) scale(0.97, 1.03);
          }
          75% {
            transform: translateY(1px) scale(1.01, 0.99);
          }
        }

        /* Micro-Rotation Head Bob */
        @keyframes patchyHeadBob {
          0%, 100% {
            transform: translateY(0px) rotate(0deg);
          }
          35% {
            transform: translateY(-2.5px) rotate(1.5deg);
          }
          70% {
            transform: translateY(1px) rotate(-1deg);
          }
        }

        /* Expanding Radar Pulse Waves */
        @keyframes patchyRadarPulse {
          0% {
            r: 4px;
            opacity: 0.9;
            stroke-width: 1.5px;
          }
          100% {
            r: 14px;
            opacity: 0;
            stroke-width: 0.5px;
          }
        }

        /* Active Visor Scan Line */
        @keyframes patchyVisorScan {
          0% {
            transform: translateY(0px);
          }
          100% {
            transform: translateY(15px);
          }
        }

        /* Approval State: Rubbing Back of Head Motion */
        @keyframes patchyBackHeadRub {
          0%, 100% {
            transform: translate(0px, 0px) rotate(0deg);
          }
          50% {
            transform: translate(-3px, 2px) rotate(-6deg);
          }
        }

        /* Approval State: Dripping Sweat Drop */
        @keyframes patchySweatDrip {
          0% {
            transform: translateY(-2px) scale(0.5);
            opacity: 0;
          }
          25% {
            transform: translateY(1px) scale(1);
            opacity: 1;
          }
          70% {
            transform: translateY(5px) scale(1);
            opacity: 0.8;
          }
          100% {
            transform: translateY(9px) scale(0.4);
            opacity: 0;
          }
        }

        /* Diagnosing / Running State: Both Hands Head Squeeze/Pulse */
        @keyframes patchyDiagnosingHands {
          0%, 100% {
            transform: translateY(0px);
          }
          50% {
            transform: translateY(-1.5px);
          }
        }

        /* Class Bindings */
        .patchy-terminal-bot-float {
          transform-origin: 50px 85px;
          animation: patchyFloat 3.8s ease-in-out infinite;
        }
        .patchy-terminal-head {
          transform-origin: 50px 37px;
          animation: patchyHeadBob 3.2s ease-in-out infinite;
        }
        .patchy-terminal-radar.radar-a {
          transform-origin: 50px 10px;
          animation: patchyRadarPulse 2.2s cubic-bezier(0.215, 0.61, 0.355, 1) infinite;
        }
        .patchy-terminal-radar.radar-b {
          transform-origin: 50px 10px;
          animation: patchyRadarPulse 2.2s cubic-bezier(0.215, 0.61, 0.355, 1) 1.1s infinite;
        }
        .patchy-terminal-visor-scan {
          animation: patchyVisorScan 1.1s ease-in-out infinite alternate;
        }
        .patchy-arm-approval-back {
          transform-origin: 76px 62px;
          animation: patchyBackHeadRub 1.2s ease-in-out infinite;
        }
        .patchy-arm-diagnosing {
          animation: patchyDiagnosingHands 1.1s ease-in-out infinite alternate;
        }
        .patchy-sweat-drop {
          transform-origin: 65px 24px;
          animation: patchySweatDrip 1.5s cubic-bezier(0.4, 0, 0.6, 1) infinite;
        }
      `}</style>

      <defs>
        <clipPath id={`patchy-terminal-visor-${activity}`}>
          <rect x="33" y="25" width="34" height="24" rx="6" />
        </clipPath>
      </defs>

      <g className="patchy-terminal-bot-float">
        <circle cx="50" cy="50" r="39" fill={glow} />

        <circle cx="50" cy="10" r="4" stroke={accent} fill="none" className="patchy-terminal-radar radar-a" />
        <circle cx="50" cy="10" r="4" stroke={accent} fill="none" className="patchy-terminal-radar radar-b" />

        <rect x="35" y="85" width="12" height="6" rx="2" fill="#8E95A2" className="patchy-terminal-foot foot-left" />
        <rect x="53" y="85" width="12" height="6" rx="2" fill="#8E95A2" className="patchy-terminal-foot foot-right" />

        <rect x="34" y="56" width="32" height="30" rx="7" fill="#121316" stroke="#2A2A32" strokeWidth="2" />
        <rect x="43" y="63" width="14" height="14" rx="3" fill="#0C1016" stroke="#1F242D" />
        <path d="M50 66 V74 M46 70 H54" stroke={accent} strokeWidth="2" strokeLinecap="round" opacity="0.7" />

        {/* Left Arm */}
        <g className={`patchy-terminal-arm arm-left ${isRunning ? 'patchy-arm-diagnosing' : ''}`}>
          <path
            d={getLeftArmPath()}
            stroke="#8E95A2"
            strokeWidth="4"
            strokeLinecap="round"
            fill="none"
          />
          <circle cx={leftHandCoords.cx} cy={leftHandCoords.cy} r="3.5" fill="#8E95A2" />
        </g>

        {/* Right Arm (Positioned before Head in DOM to render behind head) */}
        <g className={`patchy-terminal-arm arm-right ${isApproval ? 'patchy-arm-approval-back' : isRunning ? 'patchy-arm-diagnosing' : ''}`}>
          <path
            d={getRightArmPath()}
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
            cx={rightHandCoords.cx}
            cy={rightHandCoords.cy}
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

        {/* Head Assembly (Rendered on top of arms) */}
        <g className="patchy-terminal-head">
          <line x1="50" y1="20" x2="50" y2="12" stroke="#8E95A2" strokeWidth="2.5" strokeLinecap="round" />
          <circle cx="50" cy="10" r="4" fill={accent} />
          <rect x="28" y="20" width="44" height="34" rx="10" fill="#121316" stroke="#2A2A32" strokeWidth="2" />
          <rect x="23" y="32" width="5" height="10" rx="2" fill="#8E95A2" />
          <rect x="72" y="32" width="5" height="10" rx="2" fill="#8E95A2" />
          <rect x="33" y="25" width="34" height="24" rx="6" fill={visor} stroke={accent} strokeOpacity="0.34" />

          {/* Bead of Sweat (Approval State) */}
          {isApproval && (
            <path
              d="M 65 20 C 62.5 23, 62.5 27, 65 28.5 C 67.5 27, 67.5 23, 65 20 Z"
              fill="#60A5FA"
              className="patchy-sweat-drop"
            />
          )}

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
      </g>
    </svg>
  );
};

export default PatchyTerminalMascot;