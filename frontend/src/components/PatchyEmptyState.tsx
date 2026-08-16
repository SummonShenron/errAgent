// components/PatchyEmptyState.tsx
import React, { useState, useEffect, useMemo, useRef } from 'react';

export type SystemHealth = 'healthy' | 'degraded' | 'unhealthy';
export type IncidentStatus = 'idle' | 'analyzing' | 'fix_proposed' | 'resolved' | 'alert' | 'analysis_failed';

interface PatchyEmptyStateProps {
  tab: 'open' | 'resolved';
  activeIncidentsCount?: number;
  systemHealth?: SystemHealth;
  selectedIncident?: { id?: string; status?: string };
  status?: string;
  incidentStatus?: IncidentStatus;
  isAnalyzing?: boolean;
  isResolved?: boolean;
}

export const PatchyEmptyState: React.FC<PatchyEmptyStateProps> = ({ 
  tab, 
  activeIncidentsCount = 0,
  systemHealth = 'healthy',
  selectedIncident,
  status,
  incidentStatus,
  isAnalyzing = false,
  isResolved = false,
}) => {
  const [celebrationDone, setCelebrationDone] = useState(true);
  const prevStatusRef = useRef<string | null>(null);

  const hasActiveIncidents = activeIncidentsCount > 0;
  const isSystemDegradedOrUnhealthy = systemHealth === 'degraded' || systemHealth === 'unhealthy';
  const isAllClear = tab === 'open';

  // Normalize status string from selectedIncident, status prop, or legacy booleans
  const rawStatus = (
    selectedIncident?.status || 
    status || 
    incidentStatus || 
    ''
  ).toLowerCase();

  const isRawResolved = rawStatus.includes('resolv') || isResolved;
  const isRawFailed = rawStatus.includes('fail') || rawStatus.includes('error');

  const isAnActiveStatus = (s: string | null) => {
    if (!s) return false;
    return s.includes('analyz') || s.includes('fix') || s.includes('proposed') || s.includes('alert') || s.includes('active') || s.includes('open');
  };

  useEffect(() => {
    const prevStatus = prevStatusRef.current;

    if (isAnActiveStatus(prevStatus) && isRawResolved) {
      setCelebrationDone(false);
      const timer = setTimeout(() => {
        setCelebrationDone(true);
      }, 2500);
      
      return () => clearTimeout(timer);
    }

    prevStatusRef.current = rawStatus;
  }, [isRawResolved, rawStatus]);

  const currentStatus: IncidentStatus = useMemo(() => {
    if (isRawResolved && !celebrationDone) return 'resolved';
    if (isRawFailed) return 'analysis_failed';
    if (rawStatus.includes('fix') || rawStatus.includes('proposed')) return 'fix_proposed';
    if (rawStatus.includes('analyz') || isAnalyzing) return 'analyzing';
    if (hasActiveIncidents || isSystemDegradedOrUnhealthy) return 'alert';
    return 'idle';
  }, [isRawResolved, celebrationDone, isRawFailed, rawStatus, isAnalyzing, hasActiveIncidents, isSystemDegradedOrUnhealthy]);

  // Dynamic Theme Colors
  const getAccentColor = () => {
    switch (currentStatus) {
      case 'resolved': return '#22C55E';
      case 'analyzing': return '#EAB308';
      case 'fix_proposed': return '#38C2DE';
      case 'analysis_failed':
      case 'alert': return '#EF4444';
      case 'idle':
      default: return '#38C2DE';
    }
  };

  const getGlowColor = () => {
    switch (currentStatus) {
      case 'resolved': return 'rgba(34, 197, 94, 0.3)';
      case 'analyzing': return 'rgba(234, 179, 8, 0.25)';
      case 'fix_proposed': return 'rgba(56, 194, 222, 0.25)';
      case 'analysis_failed':
      case 'alert': return 'rgba(239, 68, 68, 0.25)';
      case 'idle':
      default: return 'rgba(56, 194, 222, 0.12)';
    }
  };

  const getVisorBg = () => {
    switch (currentStatus) {
      case 'resolved': return '#0d1f12';
      case 'analyzing': return '#1a160a';
      case 'fix_proposed': return '#09151c';
      case 'analysis_failed':
      case 'alert': return '#1c0d11';
      case 'idle':
      default: return '#0C1016';
    }
  };

  const getVisorBorder = () => {
    switch (currentStatus) {
      case 'resolved': return '#174722';
      case 'analyzing': return '#4a3b10';
      case 'fix_proposed': return '#16384a';
      case 'analysis_failed':
      case 'alert': return '#4a151b';
      case 'idle':
      default: return '#1F242D';
    }
  };

  const accentColor = getAccentColor();
  const glowColor = getGlowColor();
  const visorBg = getVisorBg();
  const visorBorder = getVisorBorder();

  const getHeaderTitle = () => {
    switch (currentStatus) {
      case 'resolved': return 'Incident Resolved!';
      case 'analyzing': return 'Patchy is Analyzing...';
      case 'fix_proposed': return 'Fix Proposed — Review Ready';
      case 'analysis_failed': return 'Analysis Failed';
      case 'alert': return `Active Incident Detected (${activeIncidentsCount})`;
      case 'idle':
      default:
        if (isRawResolved) return 'Incident Resolved!';
        return isAllClear ? 'All Systems Clear!' : 'No Resolved Records';
    }
  };

  const getSubtext = () => {
    switch (currentStatus) {
      case 'resolved': return 'Patchy verified the fix! Systems are stable and green.';
      case 'analyzing': return 'Parsing webhook telemetry and investigating root cause analysis.';
      case 'fix_proposed': return 'Patchy generated a hotfix. Waiting for pull request review!';
      case 'analysis_failed': return 'Patchy encountered an error while analyzing telemetry. Manual review needed.';
      case 'alert': return 'Patchy is on high alert. Select an incident from the feed to review analysis.';
      case 'idle':
      default:
        if (isRawResolved) return 'Patchy verified the fix! Systems are stable.';
        return isAllClear
          ? 'Patchy is standing by. No active incidents detected.'
          : 'Resolved incidents will appear here once hotfixes are merged.';
    }
  };

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
        /* Animations */
        @keyframes ibmFloat {
          0%, 100% { transform: translateY(0px) scale(1, 1); }
          50% { transform: translateY(-10px) scale(0.97, 1.03); }
          75% { transform: translateY(-2px) scale(1.02, 0.98); }
        }

        @keyframes ibmAlertPulse {
          0%, 100% { 
            transform: translateY(0px) scale(1);
            filter: drop-shadow(0 0 6px rgba(239, 68, 68, 0.4));
          }
          50% { 
            transform: translateY(-6px) scale(1.04);
            filter: drop-shadow(0 0 16px rgba(239, 68, 68, 0.85));
          }
        }

        @keyframes ibmVictoryJitter {
          0%, 100% { transform: translateY(0px) rotate(0deg) scale(1); }
          15% { transform: translateY(-5px) rotate(-4deg) scale(1.04); }
          30% { transform: translateY(3px) rotate(4deg) scale(0.96); }
          45% { transform: translateY(-4px) rotate(-3deg) scale(1.02); }
          60% { transform: translateY(4px) rotate(3deg) scale(0.98); }
          75% { transform: translateY(-2px) rotate(-4deg) scale(1.03); }
        }

        @keyframes ibmHeadTilt {
          0%, 100% { transform: rotate(0deg); }
          25% { transform: rotate(-7deg); }
          50% { transform: rotate(0deg); }
          75% { transform: rotate(5deg); }
        }

        @keyframes ibmBlink {
          0%, 45%, 52%, 100% { transform: scaleY(1); }
          48% { transform: scaleY(0.08); }
        }

        @keyframes ibmRadarPulse {
          0% { r: 4px; opacity: 0.9; stroke-width: 1.5px; }
          100% { r: 18px; opacity: 0; stroke-width: 0.5px; }
        }

        @keyframes ibmGlint {
          0%, 60% { transform: translateX(-50px); opacity: 0; }
          70% { opacity: 0.4; }
          85%, 100% { transform: translateX(60px); opacity: 0; }
        }
        
        @keyframes ibmFootDangle {
          0%, 100% { transform: translateY(0px); }
          50% { transform: translateY(1.5px); }
        }

        @keyframes ibmSweatDrip {
          0% { transform: translateY(0px); opacity: 0; }
          30% { opacity: 0.9; }
          80% { transform: translateY(8px); opacity: 0.8; }
          100% { transform: translateY(12px); opacity: 0; }
        }

        .ibm-bot-float { 
          animation: ${
            currentStatus === 'resolved'
              ? 'ibmVictoryJitter 0.22s ease-in-out infinite'
              : (currentStatus === 'alert' || currentStatus === 'analysis_failed')
              ? 'ibmAlertPulse 1.4s ease-in-out infinite' 
              : 'ibmFloat 4s cubic-bezier(0.45, 0, 0.55, 1) infinite'
          }; 
        }
        .ibm-bot-head { transform-origin: 50px 45px; animation: ibmHeadTilt 7s ease-in-out infinite; }
        .ibm-bot-eyes { transform-origin: center; transform-box: fill-box; animation: ibmBlink 4.2s infinite; }
        .ibm-bot-radar-1 { animation: ibmRadarPulse 2.2s ease-out infinite; }
        .ibm-bot-radar-2 { animation: ibmRadarPulse 2.2s ease-out 1.1s infinite; }
        .ibm-bot-glint { animation: ibmGlint 4s ease-in-out infinite; }
        .ibm-bot-left-foot { animation: ibmFootDangle 4s ease-in-out infinite; }
        .ibm-bot-right-foot { animation: ibmFootDangle 4s ease-in-out 0.3s infinite; }

        .ibm-bot-sweat-drop {
          animation: ibmSweatDrip 1.8s ease-in-out infinite;
        }
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
            <circle cx="50" cy="50" r="38" fill={glowColor} />

            {/* Radar Wave Emission */}
            <circle cx="50" cy="10" r="4" stroke={accentColor} fill="none" className="ibm-bot-radar-1" />
            <circle cx="50" cy="10" r="4" stroke={accentColor} fill="none" className="ibm-bot-radar-2" />

            {/* Feet */}
            <rect className="ibm-bot-left-foot" x="35" y="85" width="12" height="6" rx="2" fill="#8E95A2" />
            <rect className="ibm-bot-right-foot" x="53" y="85" width="12" height="6" rx="2" fill="#8E95A2" />

            {/* Left Arm */}
            <path
              d={currentStatus === 'resolved' ? "M 24 62 C 16 50, 14 36, 18 24" : "M24 62 C16 66, 16 76, 22 82"}
              stroke="#8E95A2"
              strokeWidth="4"
              strokeLinecap="round"
              fill="none"
            />
            <circle cx={currentStatus === 'resolved' ? 18 : 22} cy={currentStatus === 'resolved' ? 24 : 82} r="3.5" fill={currentStatus === 'resolved' ? accentColor : "#8E95A2"} />

            {/* Right Arm & Hand Dot */}
            <g>
              <path
                d={
                  currentStatus === 'resolved'
                    ? "M 76 62 C 84 50, 86 36, 82 24"
                    : currentStatus === 'analyzing'
                    ? "M 76 62 C 88 50, 84 32, 74 28"
                    : "M 76 62 C 84 68, 84 76, 78 82"
                }
                stroke="#8E95A2"
                strokeWidth="4"
                strokeLinecap="round"
                fill="none"
              >
                {currentStatus === 'idle' && (
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
                {currentStatus === 'analyzing' && (
                  <animate
                    attributeName="d"
                    dur="0.6s"
                    repeatCount="indefinite"
                    calcMode="spline"
                    keyTimes="0; 0.5; 1"
                    keySplines="0.4 0 0.6 1; 0.4 0 0.6 1"
                    values="
                      M 76 62 C 88 50, 84 32, 74 28;
                      M 76 62 C 88 44, 82 24, 72 22;
                      M 76 62 C 88 50, 84 32, 74 28
                    "
                  />
                )}
              </path>

              <circle 
                cx={
                  currentStatus === 'resolved' 
                    ? 82 
                    : currentStatus === 'analyzing'
                    ? 74
                    : (currentStatus !== 'idle' ? 78 : undefined)
                } 
                cy={
                  currentStatus === 'resolved' 
                    ? 24 
                    : currentStatus === 'analyzing'
                    ? 28
                    : (currentStatus !== 'idle' ? 82 : undefined)
                } 
                r="3.5" 
                fill={accentColor}
              >
                {currentStatus === 'idle' && (
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
                {currentStatus === 'analyzing' && (
                  <>
                    <animate
                      attributeName="cx"
                      dur="0.6s"
                      repeatCount="indefinite"
                      calcMode="spline"
                      keyTimes="0; 0.5; 1"
                      keySplines="0.4 0 0.6 1; 0.4 0 0.6 1"
                      values="74; 72; 74"
                    />
                    <animate
                      attributeName="cy"
                      dur="0.6s"
                      repeatCount="indefinite"
                      calcMode="spline"
                      keyTimes="0; 0.5; 1"
                      keySplines="0.4 0 0.6 1; 0.4 0 0.6 1"
                      values="28; 22; 28"
                    />
                  </>
                )}
              </circle>
            </g>

            {/* Torso */}
            <rect x="34" y="56" width="32" height="30" rx="7" fill="#121316" stroke="#2A2A32" strokeWidth="2" />
            
            {/* Muted Static Chest Brandmark */}
            <rect x="43" y="63" width="14" height="14" rx="3" fill="#0C1016" stroke="#1F242D" />
            <path d="M50 66 V74 M46 70 H54" stroke="#38C2DE" strokeWidth="2" strokeLinecap="round" opacity="0.6" />

            {/* Head Group */}
            <g className="ibm-bot-head">
              {/* Antenna */}
              <line x1="50" y1="20" x2="50" y2="12" stroke="#8E95A2" strokeWidth="2.5" strokeLinecap="round" />
              <circle cx="50" cy="10" r="4" fill={accentColor} />

              {/* Head Shell */}
              <rect x="28" y="20" width="44" height="34" rx="10" fill="#121316" stroke="#2A2A32" strokeWidth="2" />

              {/* Anxious Sweat Drop */}
              {currentStatus === 'fix_proposed' && (
                <path
                  className="ibm-bot-sweat-drop"
                  d="M74 22 C72 20, 70 23, 71 25 C72 27, 74 27, 75 25 C76 23, 76 22, 74 22 Z"
                  fill="#38C2DE"
                />
              )}

              {/* Ears */}
              <rect x="23" y="32" width="5" height="10" rx="2" fill="#8E95A2" />
              <rect x="72" y="32" width="5" height="10" rx="2" fill="#8E95A2" />

              {/* Visor Screen */}
              <g>
                <rect x="33" y="25" width="34" height="24" rx="6" fill={visorBg} stroke={visorBorder} />
                <g clipPath="url(#visor-screen-clip)">
                  <path d="M30 20 L34 20 L28 52 L24 52 Z" fill="#FFFFFF" className="ibm-bot-glint" />
                </g>
              </g>

              {/* Anxious Worried Eyebrows */}
              {currentStatus === 'fix_proposed' && (
                <g stroke={accentColor} strokeWidth="1.8" strokeLinecap="round">
                  <path d="M38 30 L45 32" />
                  <path d="M62 30 L55 32" />
                </g>
              )}

              {/* Blinking Eyes */}
              <g className="ibm-bot-eyes">
                <circle cx="42" cy="35" r="3.5" fill={accentColor} />
                <circle cx="58" cy="35" r="3.5" fill={accentColor} />
              </g>

              {/* Mouth Expression */}
              {currentStatus === 'resolved' ? (
                <path d="M41 40 Q50 48 59 40" stroke={accentColor} strokeWidth="2.5" strokeLinecap="round" fill="none" />
              ) : currentStatus === 'fix_proposed' ? (
                <path d="M42 43 Q46 40 50 43 T58 43" stroke={accentColor} strokeWidth="2" strokeLinecap="round" fill="none" />
              ) : currentStatus === 'analyzing' ? (
                <path d="M44 42 H56" stroke={accentColor} strokeWidth="2" strokeLinecap="round" />
              ) : (currentStatus === 'alert' || currentStatus === 'analysis_failed') ? (
                <path d="M43 43 Q50 38 57 43" stroke={accentColor} strokeWidth="2" strokeLinecap="round" fill="none" />
              ) : (
                <path d="M43 41 Q50 46 57 41" stroke={accentColor} strokeWidth="2" strokeLinecap="round" fill="none" />
              )}
            </g>
          </g>
        </svg>
      </div>

      <h3 style={{ margin: '0 0 0.35rem 0', color: accentColor, fontSize: '1.1rem', fontWeight: 700 }}>
        {getHeaderTitle()}
      </h3>
      <p className="muted" style={{ margin: 0, maxWidth: '280px', fontSize: '0.85rem' }}>
        {getSubtext()}
      </p>
    </div>
  );
};