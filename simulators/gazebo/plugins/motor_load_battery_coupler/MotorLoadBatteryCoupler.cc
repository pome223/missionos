// MotorLoadBatteryCoupler implementation. See MotorLoadBatteryCoupler.hh for
// the design and the truth-surface / boundary notes.

#include "MotorLoadBatteryCoupler.hh"

#include <algorithm>
#include <cmath>

#include <gz/common/Console.hh>
#include <gz/msgs/battery_state.pb.h>
#include <gz/plugin/Register.hh>

#include <gz/sim/Model.hh>
#include <gz/sim/Util.hh>
#include <gz/sim/components/JointVelocity.hh>

namespace boiled_claw
{
namespace
{
double SecondsOf(const std::chrono::steady_clock::duration &d)
{
  return std::chrono::duration<double>(d).count();
}
}  // namespace

MotorLoadBatteryCoupler::MotorLoadBatteryCoupler() = default;
MotorLoadBatteryCoupler::~MotorLoadBatteryCoupler() = default;

void MotorLoadBatteryCoupler::Configure(
    const gz::sim::Entity &entity,
    const std::shared_ptr<const sdf::Element> &sdf,
    gz::sim::EntityComponentManager &ecm,
    gz::sim::EventManager & /*eventMgr*/)
{
  this->model_ = entity;
  gz::sim::Model model(entity);
  if (!model.Valid(ecm))
  {
    gzerr << "[MotorLoadBatteryCoupler] must be attached to a model.\n";
    return;
  }

  auto element = std::const_pointer_cast<sdf::Element>(sdf);

  this->batteryName_ = element->Get<std::string>("battery_name", this->batteryName_).first;
  this->idlePowerW_ = element->Get<double>("idle_power_w", this->idlePowerW_).first;
  this->hoverPowerW_ = element->Get<double>("hover_power_w", this->hoverPowerW_).first;
  this->hoverRotorRadS_ =
      element->Get<double>("hover_rotor_rad_s", this->hoverRotorRadS_).first;
  this->rotorVelocitySlowdown_ =
      element->Get<double>("rotor_velocity_slowdown", this->rotorVelocitySlowdown_)
          .first;
  this->maxPowerW_ = element->Get<double>("max_power_w", this->maxPowerW_).first;
  this->capacityAh_ = element->Get<double>("capacity_ah", this->capacityAh_).first;
  this->chargeAh_ =
      element->Get<double>("initial_charge_ah", this->capacityAh_).first;
  this->voltageFullV_ =
      element->Get<double>("voltage_full_v", this->voltageFullV_).first;
  this->voltageEmptyV_ =
      element->Get<double>("voltage_empty_v", this->voltageEmptyV_).first;
  this->publishRateHz_ =
      element->Get<double>("publish_rate_hz", this->publishRateHz_).first;

  if (this->hoverRotorRadS_ <= 0.0)
    this->hoverRotorRadS_ = 700.0;
  if (this->rotorVelocitySlowdown_ <= 0.0)
    this->rotorVelocitySlowdown_ = 1.0;
  this->maxPowerW_ = std::max(this->maxPowerW_, this->idlePowerW_);
  this->hoverPowerW_ = std::max(this->hoverPowerW_, this->idlePowerW_);
  this->capacityAh_ = std::max(this->capacityAh_, 0.01);
  this->chargeAh_ = std::clamp(this->chargeAh_, 0.0, this->capacityAh_);
  if (this->publishRateHz_ <= 0.0)
    this->publishRateHz_ = 2.0;

  if (element->HasElement("rotor_joint"))
  {
    auto rotor = element->GetElement("rotor_joint");
    while (rotor)
    {
      const std::string name = rotor->Get<std::string>();
      if (!name.empty())
        this->rotorJointNames_.push_back(name);
      rotor = rotor->GetNextElement("rotor_joint");
    }
  }
  if (this->rotorJointNames_.empty())
  {
    gzwarn << "[MotorLoadBatteryCoupler] no <rotor_joint> entries; battery will "
              "discharge at idle power only.\n";
  }

  this->stateTopic_ = element->Get<std::string>("state_topic", "").first;
  if (this->stateTopic_.empty())
  {
    this->stateTopic_ = "/model/" + model.Name(ecm) + "/battery/" +
                        this->batteryName_ + "/state";
  }
  this->statePub_ =
      this->node_.Advertise<gz::msgs::BatteryState>(this->stateTopic_);

  this->publishPeriod_ = std::chrono::duration_cast<std::chrono::steady_clock::duration>(
      std::chrono::duration<double>(1.0 / this->publishRateHz_));

  gzmsg << "[MotorLoadBatteryCoupler] battery='" << this->batteryName_
        << "' topic='" << this->stateTopic_ << "' rotors="
        << this->rotorJointNames_.size() << " capacity_ah=" << this->capacityAh_
        << " idle_w=" << this->idlePowerW_ << " hover_w=" << this->hoverPowerW_
        << " hover_rad_s=" << this->hoverRotorRadS_
        << " rotor_slowdown=" << this->rotorVelocitySlowdown_
        << " max_w=" << this->maxPowerW_ << "\n";
}

void MotorLoadBatteryCoupler::ResolveRotorJoints(
    gz::sim::EntityComponentManager &ecm)
{
  gz::sim::Model model(this->model_);
  this->rotorJoints_.clear();
  for (const auto &name : this->rotorJointNames_)
  {
    const gz::sim::Entity joint = model.JointByName(ecm, name);
    if (joint == gz::sim::kNullEntity)
    {
      gzwarn << "[MotorLoadBatteryCoupler] rotor joint '" << name
             << "' not found; skipped.\n";
      continue;
    }
    if (!ecm.Component<gz::sim::components::JointVelocity>(joint))
      ecm.CreateComponent(joint, gz::sim::components::JointVelocity());
    this->rotorJoints_.push_back(joint);
  }
  this->jointsResolved_ = true;
}

double MotorLoadBatteryCoupler::VoltageAtSoc(double soc) const
{
  soc = std::clamp(soc, 0.0, 1.0);
  return this->voltageEmptyV_ +
         (this->voltageFullV_ - this->voltageEmptyV_) * soc;
}

void MotorLoadBatteryCoupler::PublishState(double loadW, double currentA)
{
  const double soc =
      this->capacityAh_ > 0.0 ? this->chargeAh_ / this->capacityAh_ : 0.0;
  gz::msgs::BatteryState msg;
  msg.set_voltage(this->VoltageAtSoc(soc));
  msg.set_current(currentA);
  msg.set_charge(this->chargeAh_);
  msg.set_capacity(this->capacityAh_);
  msg.set_percentage(std::clamp(soc, 0.0, 1.0));
  // 2 == DISCHARGING in gz.msgs.BatteryState.PowerSupplyStatus.
  msg.set_power_supply_status(gz::msgs::BatteryState::DISCHARGING);
  (void)loadW;
  this->statePub_.Publish(msg);
}

void MotorLoadBatteryCoupler::PreUpdate(
    const gz::sim::UpdateInfo &info, gz::sim::EntityComponentManager &ecm)
{
  if (info.paused)
    return;
  if (!this->jointsResolved_)
    this->ResolveRotorJoints(ecm);

  const double dtSeconds = SecondsOf(info.dt);
  if (dtSeconds <= 0.0)
    return;

  double omegaSum2 = 0.0;
  std::size_t counted = 0;
  for (const auto &joint : this->rotorJoints_)
  {
    const auto *vel = ecm.Component<gz::sim::components::JointVelocity>(joint);
    if (!vel || vel->Data().empty())
      continue;
    // gz's MulticopterMotorModel spins the visual joint at
    // (true_motor_speed / rotorVelocitySlowdownSim); recover the true speed.
    const double omega = vel->Data().front() * this->rotorVelocitySlowdown_;
    omegaSum2 += omega * omega;
    ++counted;
  }

  double loadW = this->idlePowerW_;
  if (counted > 0)
  {
    const double denom =
        static_cast<double>(counted) * this->hoverRotorRadS_ * this->hoverRotorRadS_;
    if (denom > 0.0)
      loadW += (this->hoverPowerW_ - this->idlePowerW_) * (omegaSum2 / denom);
  }
  loadW = std::clamp(loadW, this->idlePowerW_, this->maxPowerW_);

  const double soc =
      this->capacityAh_ > 0.0 ? this->chargeAh_ / this->capacityAh_ : 0.0;
  const double voltage = std::max(this->VoltageAtSoc(soc), 1.0);
  const double currentA = loadW / voltage;
  this->chargeAh_ =
      std::clamp(this->chargeAh_ - currentA * (dtSeconds / 3600.0), 0.0,
                 this->capacityAh_);

  const auto now = std::chrono::steady_clock::time_point(info.simTime);
  if (this->lastPublish_.time_since_epoch().count() == 0 ||
      (now - this->lastPublish_) >= this->publishPeriod_)
  {
    this->PublishState(loadW, currentA);
    this->lastPublish_ = now;
  }
}
}  // namespace boiled_claw

GZ_ADD_PLUGIN(
    boiled_claw::MotorLoadBatteryCoupler,
    gz::sim::System,
    boiled_claw::MotorLoadBatteryCoupler::ISystemConfigure,
    boiled_claw::MotorLoadBatteryCoupler::ISystemPreUpdate)

GZ_ADD_PLUGIN_ALIAS(boiled_claw::MotorLoadBatteryCoupler,
                    "boiled_claw::systems::MotorLoadBatteryCoupler")
