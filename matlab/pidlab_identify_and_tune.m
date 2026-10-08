function [num, den, kp, ki, kd, filterN, fitPercent, modelOutput, modelNote] = ...
    pidlab_identify_and_tune(t, u, y, sampleTime, poles, zeros, controllerType)
%PIDLAB_IDENTIFY_AND_TUNE Identify a SISO plant and tune a PID controller.
% Requires System Identification Toolbox and Control System Toolbox.

arguments
    t (:,1) double {mustBeFinite}
    u (:,1) double {mustBeFinite}
    y (:,1) double {mustBeFinite}
    sampleTime (1,1) double {mustBePositive, mustBeFinite}
    poles (1,1) double {mustBeInteger, mustBePositive}
    zeros (1,1) double {mustBeInteger, mustBeNonnegative}
    controllerType (1,:) char
end

assert(numel(t) == numel(u) && numel(u) == numel(y), ...
    "pidlab:LengthMismatch", "t, u and y must have equal lengths.");
assert(zeros < poles, "pidlab:ImproperModel", ...
    "The number of zeros must be smaller than the number of poles.");
assert(all(diff(t) > 0), "pidlab:InvalidTime", ...
    "Time values must be strictly increasing.");

% The rest of PID Lab treats the identified coefficients as a continuous-time
% G(s).  A positive Ts would make tfest return a discrete-time G(z), whose
% coefficients cannot be passed directly to the continuous PID/frequency
% analysis path.  Keep the real sampling instants as metadata while asking
% System Identification Toolbox for a continuous-time model.
rawData = iddata(y, u, [], "SamplingInstants", t);
rawData.TimeUnit = "seconds";
meanOutput = mean(y);
data = detrend(rawData, 0);

options = tfestOptions("Display", "off", "EnforceStability", true);
plant = tfest(data, poles, zeros, options);
[compared, fit] = compare(data, plant);
modelNote = "";

% A single step does not sufficiently excite an unnecessarily high-order
% model.  tfest can then return a stable but sign-inverted DC gain, which
% makes pidtune produce negative gains for a positively driven speed loop.
% If that physical sign disagrees with the measured steady-state change,
% try the next lower order and keep it only when its fit is better.
headCount = max(1, min(numel(u), floor(0.10 * numel(u))));
tailCount = headCount;
inputChange = mean(u(end-tailCount+1:end)) - mean(u(1:headCount));
outputChange = mean(y(end-tailCount+1:end)) - mean(y(1:headCount));
measuredGain = outputChange / inputChange;
estimatedGain = dcgain(plant);
if poles > 1 && isfinite(measuredGain) && abs(measuredGain) > eps && ...
        isfinite(estimatedGain) && sign(measuredGain) ~= sign(estimatedGain)
    fallbackPoles = poles - 1;
    fallbackZeros = min(zeros, fallbackPoles - 1);
    fallbackPlant = tfest(data, fallbackPoles, fallbackZeros, options);
    [~, fallbackFit] = compare(data, fallbackPlant);
    fallbackGain = dcgain(fallbackPlant);
    if isfinite(fallbackGain) && sign(measuredGain) == sign(fallbackGain) && ...
            fallbackFit(1) >= fit(1)
        plant = fallbackPlant;
        fit = fallbackFit;
        [compared, ~] = compare(data, plant);
        modelNote = sprintf("为保持稳态方向一致，模型阶次由 %d 降为 %d。", poles, fallbackPoles);
    end
end

[controller, ~] = pidtune(plant, controllerType);

[num, den] = tfdata(plant, "v");
kp = controller.Kp;
ki = controller.Ki;
kd = controller.Kd;
if isprop(controller, "Tf") && controller.Tf > 0
    filterN = 1 / controller.Tf;
else
    filterN = 0;
end
fitPercent = fit(1);
modelOutput = compared.OutputData + meanOutput;
end
