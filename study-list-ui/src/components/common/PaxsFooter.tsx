import { useState } from 'react';
import { ExternalLink, FileText, Heart, Shield, X } from 'lucide-react';

export function PaxsFooter() {
  const [termsOpen, setTermsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<'terms' | 'privacy'>('terms');

  return (
    <>
      <footer className="relative mt-8 border-t border-slate-800/80 bg-slate-950/60 backdrop-blur-md shrink-0">
        
        {/* Glowing Top Border Accent Line */}
        <div className="absolute -top-px left-0 right-0 h-px bg-gradient-to-r from-transparent via-cyan-400/60 to-transparent" />

        <div className="max-w-7xl mx-auto px-6 py-5 flex items-center justify-between gap-4 flex-wrap text-xs text-slate-400">
          
          {/* Left: Brand Mark */}
          <div className="flex items-center gap-2.5 font-medium">
            <svg width="18" height="18" viewBox="0 0 32 32" fill="none" aria-hidden="true">
              <rect x="4" y="6" width="24" height="20" rx="4" stroke="#22d3ee" strokeWidth="2" />
              <path d="M4 12h24M4 18h24M10 6v20M22 6v20" stroke="#2dd4bf" strokeWidth="1.5" />
            </svg>
            <span className="text-slate-300 font-semibold tracking-wide">PAXS Archive</span>
            <span className="text-slate-600">•</span>
            <span className="text-slate-400">clinical imaging platform</span>
          </div>

          {/* Center: Author Copyright & Links */}
          <div className="flex items-center gap-3.5 flex-wrap justify-center font-medium">
            <div className="flex items-center gap-1.5">
              <Heart className="w-3.5 h-3.5 text-pink-400 fill-pink-400/60 animate-pulse drop-shadow-[0_0_8px_rgba(244,114,182,0.65)]" />
              <span>© 2026</span>
              <span className="font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 via-teal-300 to-purple-400 animate-pulse">
                Mohamed O. Elsamani
              </span>
            </div>

            <span className="h-3.5 w-px bg-slate-800" />

            <a
              href="https://elsamanii.rf.gd"
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-cyan-400 hover:text-cyan-300 transition-colors flex items-center gap-1 hover:underline decoration-cyan-400/60 underline-offset-4"
            >
              <ExternalLink className="w-3 h-3" />
              <span>elsamanii.rf.gd</span>
            </a>

            <span className="h-3.5 w-px bg-slate-800" />

            <button
              type="button"
              onClick={() => setTermsOpen(true)}
              className="text-slate-300 hover:text-cyan-400 transition-colors flex items-center gap-1 font-mono text-[11px]"
            >
              <FileText className="w-3 h-3 text-cyan-400" />
              <span>Terms & Conditions</span>
            </button>
          </div>

          {/* Right: Animated ECG Pulse Bars */}
          <div className="flex items-center gap-1 py-1" title="PAXS DICOM Archive Active Signal">
            <span className="w-0.5 h-1.5 bg-gradient-to-b from-cyan-400 to-teal-400 rounded-full animate-bounce" />
            <span className="w-0.5 h-4 bg-gradient-to-b from-cyan-400 to-teal-400 rounded-full animate-pulse delay-75" />
            <span className="w-0.5 h-2 bg-gradient-to-b from-cyan-400 to-teal-400 rounded-full animate-bounce delay-150" />
            <span className="w-0.5 h-5 bg-gradient-to-b from-cyan-400 to-teal-400 rounded-full animate-pulse delay-200" />
            <span className="w-0.5 h-1.5 bg-gradient-to-b from-cyan-400 to-teal-400 rounded-full animate-bounce" />
          </div>
        </div>
      </footer>

      {/* Terms & Conditions Modal */}
      {termsOpen && (
        <div className="fixed inset-0 bg-slate-950/80 backdrop-blur-md z-50 flex items-center justify-center p-4 animate-in fade-in">
          <div className="w-full max-w-2xl bg-slate-900 border border-slate-800 rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[85vh]">
            
            {/* Modal Header */}
            <div className="p-5 border-b border-slate-800 flex items-center justify-between bg-slate-950/60">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-cyan-500/10 border border-cyan-500/30 flex items-center justify-center text-cyan-400">
                  <Shield className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="text-base font-bold text-white">Terms & Conditions</h3>
                  <p className="text-xs text-slate-400">DICOM Medical Image Archiving System (PACS / VNA)</p>
                </div>
              </div>
              <button
                onClick={() => setTermsOpen(false)}
                className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Modal Tabs */}
            <div className="flex border-b border-slate-800 bg-slate-950/40 px-5 text-xs font-semibold">
              <button
                onClick={() => setActiveTab('terms')}
                className={`py-3 px-4 border-b-2 transition-colors ${
                  activeTab === 'terms'
                    ? 'border-cyan-400 text-cyan-400'
                    : 'border-transparent text-slate-400 hover:text-slate-200'
                }`}
              >
                Terms & Conditions
              </button>
              <button
                onClick={() => setActiveTab('privacy')}
                className={`py-3 px-4 border-b-2 transition-colors ${
                  activeTab === 'privacy'
                    ? 'border-cyan-400 text-cyan-400'
                    : 'border-transparent text-slate-400 hover:text-slate-200'
                }`}
              >
                Privacy & PHI Policy
              </button>
            </div>

            {/* Modal Body */}
            <div className="p-6 overflow-y-auto text-xs text-slate-300 space-y-4 leading-relaxed font-sans">
              {activeTab === 'terms' ? (
                <>
                  <p>
                    <strong className="text-white">1. Diagnostic Disclaimer:</strong> This System is engineered for DICOM medical image archiving, processing, network communication, and web visualization. Primary diagnostic decisions must be conducted on validated, regulatory-compliant medical display workstations.
                  </p>
                  <p>
                    <strong className="text-white">2. System Access & Security:</strong> Access to patient imaging data is restricted to authorized medical professionals, radiologists, and administrative personnel operating under institutional credentials.
                  </p>
                  <p>
                    <strong className="text-white">3. Data Integrity & Audit Trails:</strong> All imaging transactions, DICOM C-STORE/WADO operations, and study modifications are logged into a tamper-evident audit trail with SHA-256 digital signatures.
                  </p>
                </>
              ) : (
                <>
                  <p>
                    <strong className="text-white">1. Patient Health Information (PHI):</strong> Protected Health Information (PHI) stored within this DICOM archive is protected using AES-TLS encryption in transit and encrypted storage backends.
                  </p>
                  <p>
                    <strong className="text-white">2. Access Controls & Role-Based Permissions:</strong> Access rights are governed strictly via Role-Based Access Control (RBAC) enforcing explicit scope boundaries between ADMIN, RADIOLOGIST, PHYSICIAN, and TECHNOLOGIST roles.
                  </p>
                  <p>
                    <strong className="text-white">3. Compliance:</strong> Engineered in accordance with international medical software architecture guidelines for healthcare imaging interoperability (DICOM 3.0, DICOMweb, HL7 ADT/ORM/ORU, FHIR).
                  </p>
                </>
              )}
            </div>

            {/* Modal Footer */}
            <div className="p-4 border-t border-slate-800 bg-slate-950/80 flex items-center justify-between gap-4">
              <span className="text-[11px] text-slate-500 italic flex-1">
                ⚕ PAXS Archive · Developed by Mohamed O. Elsamani (elsamanii.rf.gd)
              </span>
              <button
                onClick={() => setTermsOpen(false)}
                className="px-5 py-2 rounded-lg bg-gradient-to-r from-cyan-500 to-teal-400 text-slate-950 font-bold text-xs hover:brightness-110 shadow-lg"
              >
                I Understand
              </button>
            </div>

          </div>
        </div>
      )}
    </>
  );
}
