// components/PatchyEmptyState.tsx
import React from 'react';

interface PatchyEmptyStateProps {
  tab: 'open' | 'resolved';
}

export const PatchyEmptyState: React.FC<PatchyEmptyStateProps> = ({ tab }) => {
  const isAllClear = tab === 'open';

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '2.5rem 1rem',
      textAlign: 'center',
    }}>
      <style>{`
        /* 1. Continuous Idle Float & Squish */
        @keyframes ibmFloat {
          0%, 100% { transform: translateY(0px) scale(1, 1); }
          50% { transform: translateY(-10px) scale(0.97, 1.03); }
          75% { transform: translateY(-2px) scale(1.02, 0.98); }
        }

        /* 2. Continuous Head Tilt */
        @keyframes ibmHeadTilt {
          0%, 100% { transform: rotate(0deg); }
          25% { transform: rotate(-7deg); }
          50% { transform: rotate(0deg); }
          75% { transform: rotate(5deg); }
        }

        /* 3. Continuous Eye Blink */
        @keyframes ibmBlink {
          0%, 45%, 52%, 100% { transform: scaleY(1); }
          48% { transform: scaleY(0.08); }
        }

        /* 4. Continuous Radar Pulses */
        @keyframes ibmRadarPulse {
          0% { r: 4px; opacity: 0.9; stroke-width: 1.5px; }
          100% { r: 18px; opacity: 0; stroke-width: 0.5px; }
        }

        /* 5. Continuous Visor Glint */
        @keyframes ibmGlint {
          0%, 60% { transform: translateX(-50px); opacity: 0; }
          70% { opacity: 0.4; }
          85%, 100% { transform: translateX(60px); opacity: 0; }
        }
        
        /* 6. Subtle Foot Dangle Kick */
        @keyframes ibmFootDangle {
          0%, 100% { transform: translateY(0px); }
          50% { transform: translateY(1.5px); }
        }

        .ibm-bot-float { animation: ibmFloat 4s cubic-bezier(0.45, 0, 0.55, 1) infinite; }
        .ibm-bot-head { transform-origin: 50px 45px; animation: ibmHeadTilt 7s ease-in-out infinite; }
        .ibm-bot-eyes { transform-origin: center; transform-box: fill-box; animation: ibmBlink 4.2s infinite; }
        .ibm-bot-radar-1 { animation: ibmRadarPulse 2.2s ease-out infinite; }
        .ibm-bot-radar-2 { animation: ibmRadarPulse 2.2s ease-out 1.1s infinite; }
        .ibm-bot-glint { animation: ibmGlint 4s ease-in-out infinite; }
        .ibm-bot-left-foot { animation: ibmFootDangle 4s ease-in-out infinite; }
        .ibm-bot-right-foot { animation: ibmFootDangle 4s ease-in-out 0.3s infinite; }
      `}</style>

      <div style={{ width: '150px', height: '150px', marginBottom: '1rem' }}>
        <svg
          viewBox="0 0 100 100"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          width="100%"
          height="100%"
        >
          <defs>
            <clipPath id="visor-screen-clip">
              <rect x="33" y="25" width="34" height="24" rx="6" />
            </clipPath>
          </defs>

          {/* Main Floating Body Group */}
          <g className="ibm-bot-float">
            {/* Backdrop Glow */}
            <circle cx="50" cy="50" r="38" fill="rgba(56, 194, 222, 0.12)" />

            {/* Radar Wave Emission */}
            <circle cx="50" cy="10" r="4" stroke="#38C2DE" fill="none" className="ibm-bot-radar-1" />
            <circle cx="50" cy="10" r="4" stroke="#38C2DE" fill="none" className="ibm-bot-radar-2" />

            {/* Flat Box Feet */}
            <rect className="ibm-bot-left-foot" x="35" y="85" width="12" height="6" rx="2" fill="#8E95A2" />
            <rect className="ibm-bot-right-foot" x="53" y="85" width="12" height="6" rx="2" fill="#8E95A2" />

            {/* Left Arm (Resting on side) */}
            <path
              d="M24 62 C16 66, 16 76, 22 82"
              stroke="#8E95A2"
              strokeWidth="4"
              strokeLinecap="round"
              fill="none"
            />
            <circle cx="22" cy="82" r="3.5" fill="#8E95A2" />

            {/* Right Arm & Hand Dot (Native SVG SMIL Animation for Universal Mobile Support) */}
            <g>
              <path
                d="M 76 62 C 84 68, 84 76, 78 82"
                stroke="#8E95A2"
                strokeWidth="4"
                strokeLinecap="round"
                fill="none"
              >
                <animate
                  attributeName="d"
                  dur="8s"
                  repeatCount="indefinite"
                  calcMode="spline"
                  keyTimes="0; 0.1; 0.2; 0.3; 0.4; 0.5; 0.6; 1"
                  keySplines="
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1
                  "
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
              </path>

              <circle r="3.5" fill="#38C2DE">
                <animate
                  attributeName="cx"
                  dur="8s"
                  repeatCount="indefinite"
                  calcMode="spline"
                  keyTimes="0; 0.1; 0.2; 0.3; 0.4; 0.5; 0.6; 1"
                  keySplines="
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1
                  "
                  values="78; 92; 84; 92; 84; 92; 78; 78"
                />
                <animate
                  attributeName="cy"
                  dur="8s"
                  repeatCount="indefinite"
                  calcMode="spline"
                  keyTimes="0; 0.1; 0.2; 0.3; 0.4; 0.5; 0.6; 1"
                  keySplines="
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1;
                    0.25 1 0.5 1
                  "
                  values="82; 30; 26; 30; 26; 30; 82; 82"
                />
              </circle>
            </g>

            {/* Torso */}
            <rect x="30" y="56" width="40" height="32" rx="8" fill="#121316" stroke="#2A2A32" strokeWidth="2" />
            <rect x="42" y="64" width="16" height="16" rx="3" fill="#0C1016" stroke="#1F242D" />
            <path d="M50 67 V77 M45 72 H55" stroke="#38C2DE" strokeWidth="2.5" strokeLinecap="round" />

            {/* Head Group */}
            <g className="ibm-bot-head">
              {/* Antenna */}
              <line x1="50" y1="20" x2="50" y2="12" stroke="#8E95A2" strokeWidth="2.5" strokeLinecap="round" />
              <circle cx="50" cy="10" r="4" fill="#38C2DE" />

              {/* Head Shell */}
              <rect x="28" y="20" width="44" height="34" rx="10" fill="#121316" stroke="#2A2A32" strokeWidth="2" />

              {/* Ears */}
              <rect x="23" y="32" width="5" height="10" rx="2" fill="#8E95A2" />
              <rect x="72" y="32" width="5" height="10" rx="2" fill="#8E95A2" />

              {/* Visor Screen */}
              <g>
                <rect x="33" y="25" width="34" height="24" rx="6" fill="#0C1016" stroke="#1F242D" />
                <g clipPath="url(#visor-screen-clip)">
                  <path d="M30 20 L34 20 L28 52 L24 52 Z" fill="#FFFFFF" className="ibm-bot-glint" />
                </g>
              </g>

              {/* Blinking Eyes */}
              <g className="ibm-bot-eyes">
                <circle cx="42" cy="35" r="3.5" fill="#38C2DE" />
                <circle cx="58" cy="35" r="3.5" fill="#38C2DE" />
              </g>

              {/* Smile Mouth */}
              <path d="M43 41 Q50 46 57 41" stroke="#38C2DE" strokeWidth="2" strokeLinecap="round" fill="none" />
            </g>
          </g>
        </svg>
      </div>

      <h3 style={{ margin: '0 0 0.35rem 0', color: '#f3f4f6', fontSize: '1.1rem', fontWeight: 700 }}>
        {isAllClear ? 'All Systems Clear!' : 'No Resolved Records'}
      </h3>
      <p className="muted" style={{ margin: 0, maxWidth: '280px', fontSize: '0.85rem' }}>
        {isAllClear
          ? 'Patchy is standing by. No active incidents detected.'
          : 'Resolved incidents will appear here once hotfixes are merged.'}
      </p>
    </div>
  );
};