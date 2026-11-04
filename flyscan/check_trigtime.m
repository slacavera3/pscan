%% check_trigtime.m - is the scope really re-triggering per segment?
%
% This settles whether a drift lives in the lasers or in the acquisition.
%
% TRIGTIME is the scope's own record of when each segment triggered,
% measured against the same clock that samples the data. So it and the
% copeak walk are directly comparable, and together they pin the copeak
% recurrence in absolute terms without assuming the signal generator is
% exactly on frequency:
%
%     copeak recurrence = trigger period + walk * dt_lab          (m = 1)
%                       = (trigger period + walk * dt_lab) / m    (m copeaks
%                                                                  per trigger)
%
% Two outcomes, and they point opposite ways:
%
%   * Trigger spacing is ~100.000 us while the copeak walks 199.2 ns per
%     segment  ->  the trigger is doing its job, the recurrence really is
%     different, the fault is in the lasers.
%
%   * Trigger spacing is itself ~100.199 us  ->  the TRIGGER is slow, every
%     signal walks regardless of source, and the fault is in the scope or
%     the generator. This is what a self-triggered square wave drifting
%     would imply, and it would void the laser conclusion entirely.
%
% Written for the per-line .bin layout: little-endian float64, (n_segments,
% 2) row-major, columns [trigger_time_s, trigger_offset_s]. trigger_time is
% relative to the first trigger of the acquisition; trigger_offset is the
% sub-sample offset from trigger to first sample.

trigtime_file = 'line_0000_trigtime.bin';
sample_rate   = 10e6;
nominal_rate  = 10e3;     % what the generator is set to

fi = fopen(trigtime_file, 'r');
if fi < 0
    error(['cannot open %s. Sequence acquisitions carry a TRIGTIME block; ' ...
           'if the file is absent the acquisition was single-shot.'], ...
          trigtime_file);
end
tt = fread(fi, [2 Inf], 'float64=>double')';
fclose(fi);

t   = tt(:, 1);           % trigger time, s
off = tt(:, 2);           % sub-sample trigger offset, s
n   = numel(t);

%% spacing
d = diff(t);
fprintf('%d segments\n\n', n);
fprintf('trigger spacing (scope clock)\n');
fprintf('  mean      %.6f us\n', mean(d) * 1e6);
fprintf('  median    %.6f us\n', median(d) * 1e6);
fprintf('  std       %.1f ns\n', std(d) * 1e9);
fprintf('  min/max   %.6f / %.6f us\n', min(d) * 1e6, max(d) * 1e6);

% Slope of a straight-line fit is a far better estimate of the period than
% the mean of the differences, which only uses the endpoints effectively.
p       = polyfit((0:n-1)', t, 1);
T_trig  = p(1);
f_trig  = 1 / T_trig;
fprintf('\nfitted trigger period   %.6f us\n', T_trig * 1e6);
fprintf('fitted trigger rate     %.4f Hz\n', f_trig);
fprintf('vs nominal %.1f Hz:     %+.2f Hz (%+.0f ppm)\n', ...
        nominal_rate, f_trig - nominal_rate, ...
        1e6 * (f_trig - nominal_rate) / nominal_rate);

fprintf('\nsub-sample trigger offset\n');
fprintf('  mean      %.3f ns\n', mean(off) * 1e9);
fprintf('  spread    %.3f ns\n', (max(off) - min(off)) * 1e9);

%% missed triggers
% A gap of very nearly 2x the period is a missed trigger, not jitter.
gaps = find(d > 1.5 * T_trig);
if isempty(gaps)
    fprintf('\nno missed triggers\n');
else
    fprintf('\n%d missed trigger(s) at segment %s\n', ...
            numel(gaps), mat2str(gaps(1:min(10, end))'));
end

%% the verdict
walk_samples = 1.99203;   % <- paste from measure_walk.m for THIS line
dt_lab = 1 / sample_rate;
excess = T_trig - 1 / nominal_rate;

fprintf('\n--- where is the drift? ---\n');
fprintf('trigger period is %+.2f ns from nominal\n', excess * 1e9);
fprintf('copeak walks      %+.2f ns per segment\n', walk_samples * dt_lab * 1e9);
if abs(excess) > 0.5 * abs(walk_samples * dt_lab)
    fprintf(['-> the TRIGGER itself accounts for much of the walk. Any\n' ...
             '   signal would drift the same way. Look at the scope and\n' ...
             '   the generator, not the lasers.\n']);
else
    fprintf(['-> the trigger is close to nominal, so the walk is a real\n' ...
             '   difference in the copeak recurrence. Look at the lasers.\n']);
end

T_recur = T_trig + walk_samples * dt_lab;
fprintf('\ncopeak recurrence %.6f us -> %.4f Hz (m=1)\n', ...
        T_recur * 1e6, 1 / T_recur);
fprintf('                                 %.4f Hz (m=2)\n', 2 / T_recur);

figure;
subplot(2,1,1); plot(d * 1e6, '.');
xlabel('segment'); ylabel('spacing (us)'); title('trigger spacing');
subplot(2,1,2); plot(t - polyval(p, (0:n-1)'), '.');
xlabel('segment'); ylabel('residual (s)');
title('departure from a uniform lattice');
