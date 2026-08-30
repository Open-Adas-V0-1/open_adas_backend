package BrakingSystem {

    part def Vehicle {
        attribute stoppingDistance : ISQ::LengthValue;
    }

    part vehicle : Vehicle {
        attribute :>> stoppingDistance = 45 [SI::m];
    }

    // deliberate error 1: malformed keyword ("requirment" instead of "requirement")
    requirment def StoppingDistanceRequirement {
        doc /* The vehicle shall stop within 50 meters when braking. */
        // deliberate error 2: broken reference — UndefinedVehicle does not exist
        subject veh : UndefinedVehicle;
        require constraint { veh.stoppingDistance <= 50 [SI::m] }
    }

}
