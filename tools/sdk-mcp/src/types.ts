/** Represents a single parsed method/function from the SDK docs */
export interface SdkMethod {
  name: string;
  signature: string;
  description: string;
  params: SdkParam[];
  returnType: string;
  since: string;
  throws?: string;
  supportedDevices?: string[];
}

export interface SdkParam {
  name: string;
  type: string;
  description: string;
  subParams?: SdkParam[];
}

/** Represents a class field / instance member variable */
export interface SdkField {
  name: string;
  type: string;
  description: string;
  since: string;
}

/** Represents one enum constant table within a module */
export interface SdkEnum {
  name: string;
  since: string;
  values: SdkEnumValue[];
}

export interface SdkEnumValue {
  name: string;
  value: string;
  since: string;
  description: string;
}

/** Represents a class (e.g. Toybox.Sensor.SensorData) */
export interface SdkClass {
  name: string;
  fullName: string;
  inherits?: string;
  description: string;
  since: string;
  methods: SdkMethod[];
  fields: SdkField[];
  enums: SdkEnum[];
  childClasses?: string[];
}

/** Represents a module (e.g. Toybox.Activity) */
export interface SdkModule {
  name: string;
  fullName: string;
  description: string;
  since: string;
  methods: SdkMethod[];
  enums: SdkEnum[];
  classes: SdkClass[];
}

/** The full parsed SDK index */
export interface SdkIndex {
  sdkVersion: string;
  generatedAt: string;
  modules: SdkModule[];
}

/** A single gotcha entry */
export interface Gotcha {
  id: string;
  title: string;
  api: string;
  tags: string[];
  severity: "critical" | "high" | "medium" | "low";
  wrong: string;
  correct: string;
  codeWrong?: string;
  codeCorrect?: string;
  source: string;
}

/** Device capabilities */
export interface DeviceInfo {
  /** Canonical Connect IQ product id (matches manifest.xml iq:product id). */
  name: string;
  category: string;
  buttons: string[];
  touch: boolean;
  backMethod: string;
  displaySize?: string;
  displayType?: string;
  /** Alternate names this device answers to (e.g. friendly "forerunner965" → "fr965"). */
  aliases?: string[];
}
